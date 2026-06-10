"""Implementation node for executing Tasks using container sandbox.

This node runs AI-powered code implementation inside a podman container
for security isolation. The agent has full tool access (read, write, bash)
within the container but cannot access host systems.

Architecture:
- Container runs Deep Agents with FilesystemBackend
- Workspace is mounted at /workspace
- Agent commits changes locally
- Orchestrator (this node) handles git push after container exits
"""

import logging
from pathlib import Path

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def implement_task(state: WorkflowState) -> WorkflowState:
    """Implement a single Task using container sandbox.

    This node:
    1. Gets the current Task to implement
    2. Spawns a container with the workspace mounted
    3. Container runs Deep Agents with full tool access
    4. Container runs local tests and commits changes
    5. Orchestrator (here) handles git push after success

    Args:
        state: Current workflow state.

    Returns:
        Updated state after implementation.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    current_task = state.get("current_task_key")
    task_keys = state.get("task_keys", [])

    if not workspace_path:
        logger.error(f"No workspace for implementation on {ticket_key}")
        return {
            **state,
            "last_error": "Workspace not set up",
            "current_node": "implement_bug_fix",
        }

    # Get next task to implement if not set
    if not current_task and task_keys:
        # Get tasks for current repo
        current_repo = state.get("current_repo", "")
        repo_tasks = state.get("tasks_by_repo", {}).get(current_repo, [])
        # Find first unimplemented task
        implemented = state.get("implemented_tasks", [])
        for task_key in repo_tasks:
            if task_key not in implemented:
                current_task = task_key
                break

    if not current_task:
        logger.info(f"All tasks implemented for {ticket_key}")

        # Fallback: commit any files the container agent left uncommitted.
        # The container is responsible for committing, but this catches edge
        # cases where it exited before the final commit step.
        if workspace_path:
            branch_name = state.get("context", {}).get("branch_name", "")
            current_repo = state.get("current_repo", "")
            git = GitOperations(
                Workspace(
                    path=Path(workspace_path),
                    repo_name=current_repo,
                    branch_name=branch_name,
                    ticket_key=ticket_key,
                )
            )
            if git.has_uncommitted_changes():
                logger.warning(
                    f"Uncommitted changes found after all tasks for {ticket_key} — "
                    "committing as fallback"
                )
                # Remove the .forge/ entry setup_workspace injected into .gitignore
                # so we don't pollute the repo's gitignore with Forge internals.
                _clean_forge_gitignore(Path(workspace_path))
                git.stage_all()
                git.commit(f"[{ticket_key}] chore: commit uncommitted changes after implementation")

        return update_state_timestamp(
            {
                **state,
                "current_node": "local_review",
                "last_error": None,
            }
        )

    logger.info(f"Implementing Task {current_task} for {ticket_key}")

    settings = get_settings()
    jira = JiraClient(settings)

    try:
        # Get Task details from Jira
        task_issue = await jira.get_issue(current_task)
        task_description = task_issue.description or ""
        task_summary = task_issue.summary

        try:
            await jira.add_comment(
                ticket_key,
                f"Implementation started for [{current_task}]: {task_summary}",
            )
        except Exception:
            logger.warning(f"Failed to post implementation-started comment for {current_task}")

        # Get guardrails context
        guardrails = state.get("context", {}).get("guardrails", "")

        # Build full task description with context
        full_description = _build_task_description(
            task_summary=task_summary,
            task_description=task_description,
            guardrails=guardrails,
        )

        # Run implementation in container sandbox
        runner = ContainerRunner(settings)

        current_repo = state.get("current_repo", "")
        # Copy list to avoid mutation after passing to runner
        implemented_tasks = list(state.get("implemented_tasks", []))
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=task_summary,
            task_description=full_description,
            ticket_key=ticket_key,
            task_key=current_task,
            repo_name=current_repo,
            previous_task_keys=implemented_tasks,
        )

        if result.success:
            logger.info(f"Container completed successfully for {current_task}")

            # Track implemented tasks
            implemented = state.get("implemented_tasks", [])
            implemented.append(current_task)

            return update_state_timestamp(
                {
                    **state,
                    "current_task_key": None,
                    "implemented_tasks": implemented,
                    "current_node": "implement_bug_fix",
                    "last_error": None,
                    "retry_count": 0,
                }
            )
        else:
            # Container failed - treat all failures the same
            # The container agent is responsible for running tests and only
            # committing when they pass. If we get here, implementation failed.
            error_msg = result.error_message or "Unknown container error"
            logger.error(f"Implementation failed for {current_task}: {error_msg}")
            raise RuntimeError(error_msg)

    except Exception as e:
        logger.error(f"Implementation failed for {current_task}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "implement_task")
        return {
            **state,
            "last_error": str(e),
            "current_node": "implement_bug_fix",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


def _clean_forge_gitignore(workspace_path: Path) -> None:
    """Remove the .forge/ entry that setup_workspace injected into .gitignore.

    setup_workspace adds a .forge/ exclusion to prevent accidental commits of
    workflow state. Before the fallback commit we strip it out so the target
    repo's .gitignore isn't polluted with Forge-internal entries.
    """
    gitignore_path = workspace_path / ".gitignore"
    if not gitignore_path.exists():
        return

    content = gitignore_path.read_text()
    if ".forge" not in content:
        return

    cleaned = (
        "\n".join(
            line
            for line in content.splitlines()
            if ".forge" not in line and "Forge workflow state" not in line
        ).rstrip("\n")
        + "\n"
    )

    if cleaned != content:
        gitignore_path.write_text(cleaned)
        logger.debug("Removed .forge/ entry from .gitignore before fallback commit")


def _build_task_description(
    task_summary: str,
    task_description: str,
    guardrails: str,
) -> str:
    """Build the full task description for the container.

    Args:
        task_summary: Task title.
        task_description: Task details.
        guardrails: Project guardrails context.

    Returns:
        Full task description with all context.
    """
    parts = [
        f"# Task: {task_summary}",
        "",
        "## Description",
        task_description,
    ]

    if guardrails:
        parts.extend(
            [
                "",
                "## Project Guidelines",
                guardrails,
            ]
        )

    parts.extend(
        [
            "",
            "## Instructions",
            "1. Read and understand the existing codebase",
            "2. Implement the task following the repository's coding standards",
            "3. Write clean, well-documented code",
            "4. Run tests to verify your changes work",
            "5. Commit your changes with a descriptive message",
        ]
    )

    return "\n".join(parts)
