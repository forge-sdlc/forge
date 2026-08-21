"""Error handling utilities for workflow nodes.

Provides helpers for posting error notifications to Jira with user mentions.
"""

import logging
from typing import Any

from forge.integrations.jira.client import JiraClient
from forge.integrations.source_control.errors import SourceControlError
from forge.utils.redaction import redact_secrets
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils.source_control import get_adapter, identity_for

logger = logging.getLogger(__name__)
_MODEL_POLICY_ERROR_PREFIX = "Model policy configuration error:"
_AVAILABLE_MODELS_MARKER = ". Available connections and models: "


def _model_policy_error_parts(error: str) -> tuple[str, str]:
    detail = error.removeprefix(_MODEL_POLICY_ERROR_PREFIX).strip()
    problem, separator, available = detail.partition(_AVAILABLE_MODELS_MARKER)
    return problem.rstrip("."), available if separator else "Unavailable"


def _model_policy_fix_command(ticket_key: str, node_name: str) -> str:
    project_key = ticket_key.split("-", 1)[0].upper()
    return f"forge project-setup {project_key} --model {node_name}=CONNECTION:MODEL"


async def notify_error(
    state: WorkflowState,
    error: str,
    node_name: str,
) -> None:
    """Post an error notification comment to the ticket.

    Mentions the reporter and assignee (if set) to alert them of the failure.

    Args:
        state: Current workflow state.
        error: The error message.
        node_name: Name of the node that failed.
    """
    ticket_key = state.get("ticket_key")
    if not ticket_key:
        logger.warning("Cannot post error comment: no ticket_key in state")
        return

    jira = JiraClient()
    try:
        # Get the issue to find reporter and assignee
        issue = await jira.get_issue(ticket_key)

        # Collect account IDs to mention
        mention_ids: list[str] = []
        if issue.reporter and issue.reporter.account_id:
            mention_ids.append(issue.reporter.account_id)
        # Avoid duplicate if reporter == assignee
        if (
            issue.assignee
            and issue.assignee.account_id
            and issue.assignee.account_id not in mention_ids
        ):
            mention_ids.append(issue.assignee.account_id)

        # Redact secrets before truncation so credentials are never posted to Jira.
        safe_error = redact_secrets(error)
        error_limit = 2000 if safe_error.startswith(_MODEL_POLICY_ERROR_PREFIX) else 500
        error_truncated = (
            safe_error[:error_limit] + "..." if len(safe_error) > error_limit else safe_error
        )

        is_model_policy_error = safe_error.startswith(_MODEL_POLICY_ERROR_PREFIX)
        if is_model_policy_error:
            problem, available = _model_policy_error_parts(error_truncated)
            await jira.add_model_policy_error_comment(
                issue_key=ticket_key,
                node_name=node_name,
                problem=problem,
                available_connections=available,
                fix_command=_model_policy_fix_command(ticket_key, node_name),
                mention_account_ids=mention_ids,
            )
        else:
            await jira.add_error_comment(
                issue_key=ticket_key,
                error_message=error_truncated,
                node_name=node_name,
                mention_account_ids=mention_ids,
            )

        logger.info(f"Posted error notification to {ticket_key}")

        # When a model-policy failure occurs after a PR exists, mirror the same
        # actionable, non-secret diagnostic to GitHub for contributors who are
        # following the workflow there rather than in Jira.
        current_repo = state.get("current_repo", "")
        pr_number = state.get("current_pr_number")
        if (
            is_model_policy_error
            and isinstance(current_repo, str)
            and "/" in current_repo
            and pr_number
        ):
            try:
                repo_ref, adapter = get_adapter(current_repo)
                identity = identity_for(repo_ref, int(pr_number))
                problem, available = _model_policy_error_parts(error_truncated)
                fix_command = _model_policy_fix_command(ticket_key, node_name)
                await adapter.create_comment(
                    repo_ref,
                    identity,
                    "## 🚨 Action required: Forge model configuration\n\n"
                    "This stage stopped because its project model selection is invalid. "
                    "**The solution is below.**\n\n"
                    f"**Stage:** `{node_name}`\n\n"
                    f"**What needs fixing:** {problem}\n\n"
                    "### Available configured connections and models\n\n"
                    f"`{available}`\n\n"
                    "### Fix and retry\n\n"
                    "Choose a connection and model from the list above, then run:\n\n"
                    f"```bash\n{fix_command}\n```\n\n"
                    f"Then add the `forge:retry` label to `{ticket_key}`.",
                )
                logger.info(f"Posted model policy error to {current_repo}#{pr_number}")
            except SourceControlError as sc_error:
                logger.warning(
                    f"Failed to post model policy error to {current_repo}#{pr_number}: {sc_error}"
                )
            except Exception as unexpected_error:
                logger.warning(
                    f"Unexpected error posting model policy error to "
                    f"{current_repo}#{pr_number}: {unexpected_error}"
                )

    except Exception as e:
        # Don't fail the workflow if we can't post a comment
        logger.error(f"Failed to post error comment to {ticket_key}: {e}")
    finally:
        await jira.close()


def build_error_state(
    state: WorkflowState,
    error: str,
    node_name: str,
) -> dict[str, Any]:
    """Build an error state dict for returning from a failed node.

    Args:
        state: Current workflow state.
        error: The error message.
        node_name: Name of the node that failed.

    Returns:
        Updated state dict with error information.
    """
    return {
        **state,
        "last_error": redact_secrets(error),
        "current_node": node_name,
        "retry_count": state.get("retry_count", 0) + 1,
    }
