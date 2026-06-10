"""Planning stage nodes for bug workflow: plan_bug_fix, plan_approval_gate,
route_plan_approval, regenerate_plan, decompose_plan."""

import contextlib
import logging
import re
import tempfile
from pathlib import Path

from langgraph.graph import END

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.bug.state import BugState
from forge.workflow.utils import set_paused, update_state_timestamp

logger = logging.getLogger(__name__)

_MAX_PLAN_RETRIES = 3
_MAX_COMMENT_CHARS = 25_000
_TRUNCATION_NOTE = "*(Plan truncated — full plan available in container logs.)*"

__all__ = [
    "plan_bug_fix",
    "plan_approval_gate",
    "route_plan_approval",
    "regenerate_plan",
    "decompose_plan",
]


async def plan_bug_fix(state: BugState) -> BugState:
    """Run container to produce a concrete bug fix plan.

    Receives the RCA and selected fix approach. Container writes plan to
    .forge/plan.md. Posts plan to Jira as a comment and sets forge:plan-pending.

    Args:
        state: Current bug workflow state with rca_content and selected_fix_approach.

    Returns:
        Updated state with plan_content set and current_node=plan_approval_gate.
    """
    return await _run_plan_container(state, "plan-bug-fix", retry_node="plan_bug_fix")


async def regenerate_plan(state: BugState) -> BugState:
    """Regenerate bug fix plan based on user feedback.

    Uses regenerate-plan.md prompt with the original plan and feedback comment.
    Clears revision_requested and feedback_comment on success.

    Args:
        state: Current bug workflow state with feedback_comment set.

    Returns:
        Updated state with new plan_content, routed to plan_approval_gate.
    """
    result = await _run_plan_container(state, "regenerate-plan", retry_node="regenerate_plan")
    if result["current_node"] == "plan_approval_gate":
        return {
            **result,
            "feedback_comment": None,
            "revision_requested": False,
        }
    return result


async def _run_plan_container(
    state: BugState,
    prompt_name: str,
    retry_node: str,
) -> BugState:
    """Shared container-invocation logic for plan_bug_fix and regenerate_plan.

    Spawns a ContainerRunner with the given prompt, harvests .forge/plan.md,
    posts the plan to Jira with truncation, and sets forge:plan-pending.

    Args:
        state: Current bug workflow state.
        prompt_name: Name of the prompt template to load (without .md extension).
        retry_node: Node to route to on recoverable failure (before escalation threshold).
    """
    ticket_key = state["ticket_key"]
    retry_count = state.get("retry_count", 0)
    rca_content = state.get("rca_content") or ""
    selected_fix_approach = state.get("selected_fix_approach") or {}
    feedback_comment = state.get("feedback_comment") or ""
    original_plan = state.get("plan_content") or ""

    settings = get_settings()
    jira = JiraClient()

    try:
        issue = await jira.get_issue(ticket_key)

        # Let the user know planning is starting before the container runs
        if prompt_name == "plan-bug-fix":
            approach_title = selected_fix_approach.get("title", "selected approach")
            await jira.add_comment(
                ticket_key,
                f"Got it — working on a concrete plan for *{approach_title}*. "
                "This will take a few minutes.",
            )
        elif prompt_name == "regenerate-plan":
            await jira.add_comment(
                ticket_key,
                "Revising the plan based on your feedback — this will take a few minutes.",
            )

        # Fetch the project's known repos so the agent uses real names in repo: tags
        known_repos: list[str] = []
        with contextlib.suppress(Exception):
            known_repos = await jira.get_project_repos(issue.project_key)

        task_description = load_prompt(
            prompt_name,
            ticket_key=ticket_key,
            bug_summary=issue.summary or "",
            rca_content=rca_content,
            fix_approach_title=selected_fix_approach.get("title", ""),
            fix_approach_description=selected_fix_approach.get("description", ""),
            fix_approach_tradeoffs=selected_fix_approach.get("tradeoffs", ""),
            feedback_comment=feedback_comment,
            original_plan=original_plan,
            known_repos="\n".join(known_repos),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_path = Path(tmpdir)
            runner = ContainerRunner(settings)
            result = await runner.run(
                workspace_path=workspace_path,
                task_summary=f"Plan bug fix for {ticket_key}",
                task_description=task_description,
                ticket_key=ticket_key,
                task_key=f"{ticket_key}-plan",
            )

            if not result.success:
                raise RuntimeError(
                    f"Container failed with exit_code={result.exit_code}: {result.stderr}"
                )

            new_plan = _harvest_plan(workspace_path)

        comment = _truncate_plan_comment(new_plan)
        await jira.add_comment(ticket_key, comment)
        await jira.set_workflow_label(ticket_key, ForgeLabel.PLAN_PENDING)

        return update_state_timestamp(
            {
                **state,
                "plan_content": new_plan,
                "current_node": "plan_approval_gate",
                "last_error": None,
                "retry_count": 0,
            }
        )

    except Exception as e:
        logger.error(f"_run_plan_container ({prompt_name}) failed for {ticket_key}: {e}")
        new_retry = retry_count + 1
        next_node = "escalate_blocked" if new_retry >= _MAX_PLAN_RETRIES else retry_node
        return {
            **state,
            "last_error": str(e),
            "current_node": next_node,
            "retry_count": new_retry,
        }

    finally:
        await jira.close()


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


def plan_approval_gate(state: BugState) -> BugState:
    """Pause and wait for plan approval.

    Args:
        state: Current bug workflow state.

    Returns:
        State with is_paused=True and current_node=plan_approval_gate.
    """
    return set_paused(state, "plan_approval_gate")


def route_plan_approval(state: BugState) -> str:
    """Route after plan approval gate resumes.

    Checks state flags in priority order:
    1. is_question → answer_question
    2. is_paused → END
    3. revision_requested → regenerate_plan
    4. (otherwise, plan approved) → decompose_plan

    Args:
        state: Current bug workflow state.

    Returns:
        Name of next node or END.
    """
    if state.get("is_question"):
        return "answer_question"

    if state.get("is_paused"):
        return END

    if state.get("revision_requested"):
        return "regenerate_plan"

    return "decompose_plan"


async def decompose_plan(state: BugState) -> BugState:
    """Decompose approved plan into per-repo Jira tasks.

    No container. Creates one task per repo identified in plan_content.
    Idempotent: reuses existing tasks with matching repo: labels.
    Atomic: escalates if any new task creation fails.

    Args:
        state: Current bug workflow state with plan_content set.

    Returns:
        Updated state with linked_task_keys populated and current_node=setup_workspace.
    """
    ticket_key = state["ticket_key"]
    plan_content = state.get("plan_content") or ""
    rca_content = state.get("rca_content") or ""
    selected_fix_approach = state.get("selected_fix_approach") or {}
    repos = list(
        dict.fromkeys(
            re.sub(r"[^a-zA-Z0-9/._-]", "", r).rstrip(".")
            for r in re.findall(r"repo:(\S+)", plan_content)
        )
    )

    jira = JiraClient()

    try:
        issue = await jira.get_issue(ticket_key)
        project_key = issue.project_key
        bug_summary = issue.summary or ticket_key

        if not repos:
            # Fall back to the project's configured repos from Jira.
            # This handles plans that don't include explicit repo: tags.
            with contextlib.suppress(Exception):
                project_repos = await jira.get_project_repos(issue.project_key)
                repos = project_repos[:1]  # one task for the primary repo
            if not repos:
                logger.warning(
                    f"No repo: tags in plan and no project repos configured for "
                    f"{ticket_key} — skipping task creation"
                )
                return update_state_timestamp(
                    {**state, "current_node": "setup_workspace", "last_error": None}
                )

        # Idempotency: check existing Relates links for tasks already created.
        # Suppress per-task label fetch errors — if a linked issue is deleted/archived,
        # skip it rather than blocking the entire decomposition.
        existing_links = await jira.get_issue_links(ticket_key)
        covered: dict[str, str] = {}
        for link in existing_links:
            if link.get("type", "").lower() in (
                "relates",
                "related",
                "relates to",
                "is related to",
            ):
                linked_key = link.get("inward_key") or link.get("outward_key")
                if linked_key:
                    with contextlib.suppress(Exception):
                        labels = await jira.get_labels(linked_key)
                        for lbl in labels:
                            if lbl.startswith("repo:"):
                                covered[lbl[5:]] = linked_key

        tasks_by_repo: dict[str, list[str]] = {}
        all_task_keys: list[str] = []

        for repo in repos:
            if repo in covered:
                task_key = covered[repo]
            else:
                scoped_description = (
                    f"## Bug Fix Plan\n\n{plan_content}\n\n"
                    f"## Root Cause\n\n{rca_content}\n\n"
                    f"## Selected Approach\n\n"
                    f"**{selected_fix_approach.get('title', '')}**: "
                    f"{selected_fix_approach.get('description', '')}\n\n"
                    f"**Scope:** This task covers changes to `{repo}` only."
                )

                task_key = await jira.create_task(
                    project_key=project_key,
                    summary=f"Fix: {bug_summary} ({repo})",
                    description=scoped_description,
                    labels=[
                        f"repo:{repo}",
                        ForgeLabel.FORGE_MANAGED.value,
                        f"forge:parent:{ticket_key}",
                    ],
                )
                await jira.create_issue_link("Related", task_key, ticket_key)

            tasks_by_repo[repo] = [task_key]
            all_task_keys.append(task_key)

        # repos_to_process drives teardown_and_route's multi-repo iteration.
        repos_to_process = repos
        return update_state_timestamp(
            {
                **state,
                "linked_task_keys": all_task_keys,
                "task_keys": all_task_keys,
                "repos_to_process": repos_to_process,
                "tasks_by_repo": tasks_by_repo,
                "current_repo": repos_to_process[0] if repos_to_process else None,
                "current_task_key": all_task_keys[0] if all_task_keys else None,
                "current_node": "setup_workspace",
                "last_error": None,
                "retry_count": 0,
            }
        )

    except Exception as e:
        logger.error(f"decompose_plan failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "escalate_blocked",
        }

    finally:
        await jira.close()
