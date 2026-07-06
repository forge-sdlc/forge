"""Planning node for Task Takeover workflow."""

import contextlib
import logging
import re
from typing import Any, cast

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import set_paused, update_state_timestamp

logger = logging.getLogger(__name__)

_MAX_COMMENT_CHARS = 25_000
_TRUNCATION_NOTE = "*(Plan truncated — full plan retained in workflow state.)*"

__all__ = ["generate_plan", "plan_approval_gate", "route_plan_approval"]


def _extract_plan_repos(plan_content: str, known_repos: list[str]) -> list[str]:
    """Extract valid repo tags from plan content in first-seen order."""
    allowed = set(known_repos)
    repos = []
    for repo in re.findall(r"repo:([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", plan_content):
        if allowed and repo not in allowed:
            continue
        if repo not in repos:
            repos.append(repo)
    return repos


def _repo_labels(repos: list[str]) -> list[str]:
    """Build Jira repo labels for valid repository names."""
    return [f"repo:{repo}" for repo in repos if repo and "/" in repo]


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
    agent = ForgeAgent(settings)

    try:
        issue = await jira.get_issue(ticket_key)
        comments = await jira.get_comments(ticket_key)
        comment_text = "\n\n".join(c.body for c in comments if c.body)

        # Notify Jira before planning starts
        if is_revision:
            await jira.add_comment(
                ticket_key,
                "Revising the plan based on your feedback — this will take a few minutes.",
            )
        else:
            await jira.add_comment(
                ticket_key,
                "Starting implementation plan generation — reviewing ticket context and drafting the plan. This will take a few minutes.",
            )

        # 1. Load project's known repos. Do not pick the first repo as a fallback:
        # task takeover planning must choose a repo from the ticket context and
        # generated plan, otherwise multi-repo Jira projects attach unrelated repos.
        known_repos: list[str] = []
        with contextlib.suppress(Exception):
            known_repos = await jira.get_project_repos(issue.project_key)

        if not known_repos:
            raise ValueError(f"No repositories configured for project {issue.project_key}")

        # 2. Formulate prompt
        task_description = load_prompt(
            "task-takeover-planning",
            ticket_key=ticket_key,
            summary=issue.summary or "",
            description=issue.description or "",
            comments=comment_text,
            known_repos="\n".join(known_repos),
            file_metadata=(
                "No local repository clone is provided during planning. Use the "
                "available repository/GitHub tools to identify the correct repo "
                "and inspect only the files needed to produce a grounded plan."
            ),
        )

        # If this is a revision, append the feedback details to task_description
        if is_revision:
            task_description += f"\n\n## Revision Request\nThis is a revision request. Please update the original plan based on the feedback below.\n\n### Original Plan\n{original_plan}\n\n### Feedback Comment\n{feedback_comment}\n"

        # 3. Generate the plan directly with the planning agent. This mirrors
        # feature workflow planning and lets the agent use read-only repository
        # tools instead of requiring a cloned container workspace.
        raw_plan = await agent.run_task(
            task="task-takeover-planning",
            prompt=task_description,
            context={
                "ticket_key": ticket_key,
                "project_key": issue.project_key,
                "current_repo": state.get("current_repo") or "",
                "available_repos": known_repos,
            },
        )
        new_plan = agent._strip_preamble(raw_plan).strip()
        if not new_plan:
            raise ValueError("Planning agent returned an empty plan")

        plan_repos = _extract_plan_repos(new_plan, known_repos)
        if not plan_repos:
            raise ValueError(
                "Generated plan did not include a valid repo:<owner>/<repo> tag from "
                "the configured project repositories."
            )

        # 4. Post the plan to Jira
        truncated_comment = _truncate_plan_comment(new_plan)
        await jira.add_comment(ticket_key, truncated_comment)
        await jira.add_labels(ticket_key, _repo_labels(plan_repos))
        await jira.set_workflow_label(ticket_key, ForgeLabel.PLAN_PENDING)

        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **state,
                    "plan_content": new_plan,
                    "current_repo": plan_repos[0],
                    "repos_to_process": plan_repos,
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
        await jira.close()
        await agent.close()


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
