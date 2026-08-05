"""Triage node for Task Takeover workflow.

Evaluates whether a Task or Epic ticket contains sufficient actionable detail
before starting plan generation.
"""

import json
import logging
from typing import cast

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.workflow.task_takeover.state import TaskTakeoverState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.repo_resolution import resolve_current_repo

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3

__all__ = ["triage_task"]


async def triage_task(state: TaskTakeoverState) -> TaskTakeoverState:
    """Evaluate a Task Takeover ticket for completeness before planning.

    Posts an acknowledgement comment on first execution, then evaluates the
    ticket for enough actionable context to safely generate an implementation
    plan. Formal sections are helpful, but not required for small, contained
    tasks when the intent, scope, and expected outcome are already clear.

    If sufficient, transitions current_node to generate_plan and proceeds.
    If missing sections, applies forge:task-triage-pending label, posts a
    detailed public comment, sets is_paused = True, and routes to triage_gate.

    On resume, re-evaluates the updated ticket and proceeds to planning if now
    sufficient.

    Args:
        state: Current TaskTakeoverState.

    Returns:
        Updated TaskTakeoverState.
    """
    ticket_key = state["ticket_key"]
    retry_count = state.get("retry_count", 0)
    is_resume = state.get("current_node") == "triage_gate"

    settings = get_settings()
    jira = JiraClient(settings)
    agent = ForgeAgent(settings)

    try:
        if retry_count >= _MAX_RETRIES:
            logger.error("triage_task exceeded max retries for %s", ticket_key)
            return cast(
                TaskTakeoverState,
                {
                    **state,
                    "current_node": "escalate_blocked",
                    "is_paused": False,
                },
            )

        # Step 1: Post acknowledgement on first execution only (not on resume)
        if not is_resume:
            await post_status_comment(
                jira,
                ticket_key,
                "Received this task/epic — checking ticket completeness before starting planning.",
            )

        # Step 2: Fetch full ticket content
        issue = await jira.get_issue(ticket_key)
        comments = await jira.get_comments(ticket_key)
        comment_text = "\n\n".join(c.body for c in comments if c.body)
        current_repo, _known_repos = await resolve_current_repo(
            jira,
            issue,
            comment_text,
            state.get("current_repo"),
        )

        # Step 3: Invoke task takeover triage prompt
        user_prompt = load_prompt(
            "task-takeover-triage",
            summary=issue.summary or "",
            description=issue.description or "",
            comments=comment_text,
        )
        raw_result = await agent.run_task(
            task="task-takeover-triage",
            policy_key="task_takeover_triage",
            prompt=user_prompt,
            context={"ticket_key": ticket_key},
        )

        # Step 4: Parse result
        result_stripped = raw_result.strip()
        if result_stripped.lower() == "sufficient":
            if current_repo and "/" in current_repo:
                await jira.add_labels(ticket_key, [f"repo:{current_repo}"])

            pass_msg = (
                "Thanks for the update — ticket now has enough information to proceed. "
                "Starting plan generation — results will be posted here."
                if is_resume
                else "Ticket has enough information to proceed. Starting plan generation — results will be posted here."
            )
            await post_status_comment(jira, ticket_key, pass_msg)
            return cast(
                TaskTakeoverState,
                update_state_timestamp(
                    {
                        **state,
                        "triage_passed": True,
                        "triage_missing_fields": [],
                        "current_node": "generate_plan",
                        "is_paused": False,
                        "is_question": False,
                        "revision_requested": False,
                        "feedback_comment": None,
                        "current_repo": current_repo,
                        "last_error": None,
                        "retry_count": 0,
                    }
                ),
            )

        # Step 5: Missing fields path
        # Strip markdown code fences that LLMs sometimes add despite instructions
        json_candidate = result_stripped
        if json_candidate.startswith("```"):
            lines = json_candidate.splitlines()
            json_candidate = "\n".join(line for line in lines if not line.startswith("```")).strip()
        try:
            missing_fields = json.loads(json_candidate)
            if not isinstance(missing_fields, list):
                raise ValueError("Expected a list")
        except (json.JSONDecodeError, ValueError):
            logger.warning("Unexpected triage output for %s: %r", ticket_key, result_stripped)
            missing_fields = [
                "(could not determine — please provide additional context about the task)"
            ]

        fields_listed = "\n".join(f"- {f}" for f in missing_fields)
        await post_status_comment(
            jira,
            ticket_key,
            "To proceed with planning, please reply with a comment starting "
            f"with `!` and provide the following information:\n\n{fields_listed}",
        )
        await jira.set_workflow_label(ticket_key, ForgeLabel.TASK_TRIAGE_PENDING)

        return cast(
            TaskTakeoverState,
            update_state_timestamp(
                {
                    **state,
                    "triage_passed": False,
                    "triage_missing_fields": missing_fields,
                    "current_node": "triage_gate",
                    "is_paused": True,
                    "last_error": None,
                    "retry_count": 0,
                }
            ),
        )

    except Exception as e:
        logger.error("triage_task failed for %s: %s", ticket_key, e)
        new_retry = retry_count + 1
        return cast(
            TaskTakeoverState,
            {
                **state,
                "last_error": str(e),
                "retry_count": new_retry,
                "current_node": "escalate_blocked" if new_retry >= _MAX_RETRIES else "triage_check",
                "is_paused": False,
            },
        )
    finally:
        await jira.close()
        await agent.close()
