"""Utilities for posting workflow status updates to Jira with error suppression.

This module provides shared utilities for updating Jira issues during workflow
execution. All functions suppress errors to prevent workflow failures from Jira
API issues, while logging warnings for observability.
"""

import logging

from forge.integrations.jira import JiraClient
from forge.models.workflow import ForgeLabel

logger = logging.getLogger(__name__)


async def post_status_comment(
    jira_client: JiraClient,
    issue_key: str,
    message: str,
) -> None:
    """Post a workflow status comment to a Jira issue.

    This function suppresses all exceptions to prevent Jira API failures from
    blocking workflow execution. Errors are logged at WARNING level for
    observability.

    Args:
        jira_client: JiraClient instance for API calls.
        issue_key: The Jira issue key (e.g., "PROJ-123").
        message: Status message text to post as a comment.

    Returns:
        None. Exceptions are suppressed and logged.
    """
    try:
        await jira_client.add_comment(issue_key, message)
    except Exception as e:
        logger.warning(f"Failed to post status comment to {issue_key}: {e}")


async def transition_tasks_to_in_progress(
    jira_client: JiraClient,
    task_keys: list[str],
) -> None:
    """Transition multiple task issues to "In Progress" status.

    Iterates through tasks and attempts to transition each to "In Progress".
    Handles errors per task to ensure one failure doesn't block others.
    Logs success and failure for each task individually.

    Args:
        jira_client: JiraClient instance for API calls.
        task_keys: List of Jira task keys to transition.

    Returns:
        None. Continues processing remaining tasks on individual failures.
    """
    for task_key in task_keys:
        try:
            await jira_client.transition_issue(task_key, "In Progress")
            logger.info(f"Transitioned {task_key} to In Progress")
        except ValueError as e:
            # Transition not available for this task
            logger.warning(f"Cannot transition {task_key} to In Progress: {e}")
        except Exception as e:
            # Other API errors
            logger.warning(f"Failed to transition {task_key} to In Progress: {e}")


async def set_implementing_label(
    jira_client: JiraClient,
    feature_key: str,
) -> None:
    """Set the forge:implementing label on a feature issue.

    This function suppresses all exceptions to prevent Jira API failures from
    blocking workflow execution. Errors are logged at WARNING level.

    Args:
        jira_client: JiraClient instance for API calls.
        feature_key: The Jira feature/epic key to label.

    Returns:
        None. Exceptions are suppressed and logged.
    """
    try:
        await jira_client.set_workflow_label(
            feature_key,
            ForgeLabel.TASK_IMPLEMENTING,
        )
    except Exception as e:
        logger.warning(f"Failed to set implementing label on {feature_key}: {e}")


async def remove_implementing_label(
    jira_client: JiraClient,
    feature_key: str,
) -> None:
    """Remove the forge:implementing label from a feature issue.

    This function suppresses all exceptions to prevent Jira API failures from
    blocking workflow execution. Errors are logged at WARNING level.

    Args:
        jira_client: JiraClient instance for API calls.
        feature_key: The Jira feature/epic key to remove label from.

    Returns:
        None. Exceptions are suppressed and logged.
    """
    try:
        await jira_client.remove_labels(feature_key, [ForgeLabel.TASK_IMPLEMENTING.value])
        logger.info(f"Removed forge:implementing label from {feature_key}")
    except Exception as e:
        logger.warning(f"Failed to remove implementing label from {feature_key}: {e}")


async def set_ci_pending_label(
    jira_client: JiraClient,
    feature_key: str,
) -> None:
    """Set the forge:ci-pending label on a feature issue.

    This function suppresses all exceptions to prevent Jira API failures from
    blocking workflow execution. Errors are logged at WARNING level.

    Args:
        jira_client: JiraClient instance for API calls.
        feature_key: The Jira feature/epic key to label.

    Returns:
        None. Exceptions are suppressed and logged.
    """
    try:
        await jira_client.set_workflow_label(
            feature_key,
            ForgeLabel.TASK_CI_PENDING,
        )
        logger.info(f"Set forge:ci-pending label on {feature_key}")
    except Exception as e:
        logger.warning(f"Failed to set ci-pending label on {feature_key}: {e}")
