"""Error handling utilities for workflow nodes.

Provides helpers for posting error notifications to Jira with user mentions.
"""

import logging
from typing import Any

from forge.integrations.jira.client import JiraClient
from forge.utils.redaction import redact_secrets
from forge.workflow.feature.state import FeatureState as WorkflowState

logger = logging.getLogger(__name__)
_MODEL_POLICY_ERROR_PREFIX = "Model policy configuration error:"


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
            safe_error.startswith(_MODEL_POLICY_ERROR_PREFIX)
            and isinstance(current_repo, str)
            and "/" in current_repo
            and pr_number
        ):
            from forge.integrations.github.client import GitHubClient

            owner, repo = current_repo.split("/", 1)
            github = GitHubClient()
            try:
                await github.create_issue_comment(
                    owner,
                    repo,
                    int(pr_number),
                    "## Forge model configuration error\n\n"
                    f"{error_truncated}\n\n"
                    "Update the Jira project model policy, then retry the workflow.",
                )
                logger.info(f"Posted model policy error to {current_repo}#{pr_number}")
            except Exception as github_error:
                logger.warning(
                    f"Failed to post model policy error to {current_repo}#{pr_number}: "
                    f"{github_error}"
                )
            finally:
                await github.close()

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
