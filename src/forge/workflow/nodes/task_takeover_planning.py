"""Planning node for Task Takeover workflow."""

import contextlib
import logging
from pathlib import Path
from typing import Any, cast

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.sandbox.runner import ContainerConfig, ContainerRunner
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import set_paused, update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

_MAX_COMMENT_CHARS = 25_000
_TRUNCATION_NOTE = "*(Plan truncated — full plan available in container logs.)*"

__all__ = ["generate_plan", "plan_approval_gate", "route_plan_approval"]


def _gather_file_metadata(workspace_path: Path) -> str:
    """Gather file structure and metadata from the cloned workspace."""
    lines = []
    ignore_dirs = {
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".ruff_cache",
        ".forge",
    }

    count = 0
    max_files = 300
    for path in sorted(workspace_path.rglob("*")):
        try:
            # Skip if any part is ignored
            if any(part in ignore_dirs for part in path.relative_to(workspace_path).parts):
                continue
        except ValueError:
            continue

        if path.is_file():
            # Skip common binary/unwanted extensions
            if path.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".ico",
                ".pyc",
                ".pdf",
                ".zip",
                ".tar",
                ".gz",
                ".woff",
                ".woff2",
                ".ttf",
                ".eot",
            }:
                continue

            try:
                rel_path = path.relative_to(workspace_path)
                size = path.stat().st_size
                lines.append(f"- {rel_path} ({size} bytes)")
                count += 1
                if count >= max_files:
                    lines.append(f"- ... and more files (truncated at {max_files} files)")
                    break
            except Exception:
                continue

    if not lines:
        return "No files found in workspace."
    return "\n".join(lines)


def _truncate_plan_comment(plan_content: str, max_chars: int = _MAX_COMMENT_CHARS) -> str:
    """Truncate plan comment at last paragraph boundary before the character limit."""
    if len(plan_content) <= max_chars:
        return plan_content

    available = max_chars - len(_TRUNCATION_NOTE) - 4
    truncated = plan_content[:available]
    last_para = truncated.rfind("\n\n")
    if last_para > 0:
        truncated = truncated[:last_para]

    return truncated + "\n\n" + _TRUNCATION_NOTE


def _harvest_plan(workspace_path: Path) -> str:
    """Read .forge/plan.md from the container workspace.

    Raises:
        FileNotFoundError: if plan.md was not written.
        ValueError: if plan.md is empty.
    """
    plan_file = workspace_path / ".forge" / "plan.md"
    if not plan_file.exists():
        raise FileNotFoundError(f"plan.md not found at {plan_file}")
    content = plan_file.read_text()
    if not content.strip():
        raise ValueError("plan.md is empty")
    return content


async def generate_plan(state: TaskTakeoverState) -> TaskTakeoverState:
    """Generate or regenerate task takeover plan.

    Args:
        state: Current TaskTakeoverState.

    Returns:
        Updated TaskTakeoverState.
    """
    ticket_key = state["ticket_key"]
    retry_count = state.get("retry_count", 0)
    is_revision = (
        state.get("revision_requested", False) or state.get("feedback_comment") is not None
    )
    feedback_comment = state.get("feedback_comment") or ""
    original_plan = state.get("plan_content") or ""

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        issue = await jira.get_issue(ticket_key)
        comments = await jira.get_comments(ticket_key)
        comment_text = "\n\n".join(c.body for c in comments if c.body)

        # Notify Jira before we start container
        if is_revision:
            await jira.add_comment(
                ticket_key,
                "Revising the plan based on your feedback — this will take a few minutes.",
            )
        else:
            await jira.add_comment(
                ticket_key,
                "Starting implementation plan generation — gathering codebase metadata and drafting the plan. This will take a few minutes.",
            )

        # 1. Determine and clone/checkout repository
        current_repo = state.get("current_repo")
        if not current_repo:
            with contextlib.suppress(Exception):
                current_repo = await jira.get_project_default_repo(issue.project_key)
            if not current_repo:
                with contextlib.suppress(Exception):
                    repos = await jira.get_project_repos(issue.project_key)
                    if repos:
                        current_repo = repos[0]

        if not current_repo or current_repo == "unknown" or "/" not in current_repo:
            raise ValueError(f"No valid repository found for project {issue.project_key}")

        # Update current_repo in state
        state = cast(TaskTakeoverState, {**state, "current_repo": current_repo})

        # 2. Get Workspace and clone if needed
        workspace_manager = WorkspaceManager(base_dir=settings.workspace_base_dir)
        workspace = workspace_manager.create_workspace(
            repo_name=current_repo,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)
        if not (workspace.path / ".git").exists():
            git.clone()

        # 3. Gather repository file structure & metadata
        file_metadata = _gather_file_metadata(workspace.path)

        # 4. Load project's known repos
        known_repos: list[str] = []
        with contextlib.suppress(Exception):
            known_repos = await jira.get_project_repos(issue.project_key)
        if not known_repos:
            known_repos = [current_repo]

        # 5. Formulate prompt
        task_description = load_prompt(
            "task-takeover-planning",
            ticket_key=ticket_key,
            summary=issue.summary or "",
            description=issue.description or "",
            comments=comment_text,
            known_repos="\n".join(known_repos),
            file_metadata=file_metadata,
        )

        # If this is a revision, append the feedback details to task_description
        if is_revision:
            task_description += f"\n\n## Revision Request\nThis is a revision request. Please update the original plan based on the feedback below.\n\n### Original Plan\n{original_plan}\n\n### Feedback Comment\n{feedback_comment}\n"

        # 6. Run container with ContainerRunner (skipping tests for planning speed)
        runner = ContainerRunner(settings)
        config = ContainerConfig(skip_tests=True)
        result = await runner.run(
            workspace_path=workspace.path,
            task_summary=f"Plan task takeover for {ticket_key}",
            task_description=task_description,
            config=config,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-plan",
        )

        if not result.success:
            raise RuntimeError(
                f"Container failed with exit_code={result.exit_code}: {result.stderr}"
            )

        new_plan = _harvest_plan(workspace.path)

        # 7. Post the plan to Jira
        truncated_comment = _truncate_plan_comment(new_plan)
        await jira.add_comment(ticket_key, truncated_comment)
        await jira.set_workflow_label(ticket_key, ForgeLabel.PLAN_PENDING)

        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **state,
                    "plan_content": new_plan,
                    "current_node": "task_plan_approval_gate",
                    "last_error": None,
                    "retry_count": 0,
                    "feedback_comment": None,
                    "revision_requested": False,
                }
            ),
        )

    except Exception as e:
        logger.error(f"generate_plan failed for {ticket_key}: {e}")
        new_retry = retry_count + 1
        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **state,
                    "last_error": str(e),
                    "current_node": "generate_plan",
                    "retry_count": new_retry,
                }
            ),
        )
    finally:
        if (
            "workspace" in locals()
            and workspace
            and "workspace_manager" in locals()
            and workspace_manager
        ):
            workspace_manager.destroy_workspace(workspace)
        await jira.close()


def plan_approval_gate(state: TaskTakeoverState) -> TaskTakeoverState:
    """Pause and wait for plan approval.

    Args:
        state: Current task takeover workflow state.

    Returns:
        State with is_paused=True and current_node=plan_approval_gate.
    """
    return cast(TaskTakeoverState, set_paused(cast(dict[str, Any], state), "plan_approval_gate"))


def route_plan_approval(state: TaskTakeoverState) -> str:
    """Route after plan approval gate resumes.

    Checks state flags:
    1. is_paused -> END
    2. revision_requested -> generate_plan
    3. (otherwise, approved) -> END

    Args:
        state: Current TaskTakeoverState.

    Returns:
        Name of next node or END.
    """
    from langgraph.graph import END

    if state.get("is_paused"):
        return END

    if state.get("revision_requested"):
        return "generate_plan"

    return END
