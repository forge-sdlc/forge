"""Tests for actionable model-policy workflow notifications."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.nodes.error_handler import notify_error


@pytest.mark.asyncio
async def test_model_policy_error_is_reported_to_jira_and_active_github_pr() -> None:
    jira = MagicMock()
    issue = MagicMock(reporter=None, assignee=None)
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_model_policy_error_comment = AsyncMock()
    jira.close = AsyncMock()
    github = MagicMock()
    github.create_issue_comment = AsyncMock()
    github.close = AsyncMock()
    error = (
        "Model policy configuration error: model 'missing' is not allowed on "
        "connection 'vertex'. Available connections and models: "
        "vertex-ai: vertex=[gemini-3.5-flash, claude-sonnet-5]"
    )
    state = {
        "ticket_key": "PROJ-1",
        "current_repo": "forge-sdlc/forge",
        "current_pr_number": 251,
    }

    with (
        patch("forge.workflow.nodes.error_handler.JiraClient", return_value=jira),
        patch("forge.integrations.github.client.GitHubClient", return_value=github),
    ):
        await notify_error(state, error, "implement_task")

    guidance = jira.add_model_policy_error_comment.await_args.kwargs
    assert guidance["problem"] == ("model 'missing' is not allowed on connection 'vertex'")
    assert guidance["available_connections"] == (
        "vertex-ai: vertex=[gemini-3.5-flash, claude-sonnet-5]"
    )
    assert guidance["fix_command"] == (
        "forge project-setup PROJ --model implement_task=CONNECTION:MODEL"
    )
    github.create_issue_comment.assert_awaited_once()
    assert "vertex-ai: vertex=" in github.create_issue_comment.await_args.args[3]
    github.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_model_error_is_not_mirrored_to_github() -> None:
    jira = MagicMock()
    jira.get_issue = AsyncMock(return_value=MagicMock(reporter=None, assignee=None))
    jira.add_error_comment = AsyncMock()
    jira.close = AsyncMock()

    with (
        patch("forge.workflow.nodes.error_handler.JiraClient", return_value=jira),
        patch("forge.integrations.github.client.GitHubClient") as github_type,
    ):
        await notify_error(
            {
                "ticket_key": "PROJ-1",
                "current_repo": "forge-sdlc/forge",
                "current_pr_number": 251,
            },
            "ordinary failure",
            "implement_task",
        )

    github_type.assert_not_called()
