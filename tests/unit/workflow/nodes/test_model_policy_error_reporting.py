"""Tests for actionable model-policy workflow notifications."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import Provider, RepositoryRef
from forge.integrations.source_control.errors import NotFoundError
from forge.workflow.nodes.error_handler import notify_error


@pytest.mark.asyncio
async def test_model_policy_error_is_reported_to_jira_and_active_github_pr() -> None:
    jira = MagicMock()
    issue = MagicMock(reporter=None, assignee=None)
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_model_policy_error_comment = AsyncMock()
    jira.close = AsyncMock()
    adapter = AsyncMock()
    repo_ref = RepositoryRef(
        id="forge-sdlc/forge",
        provider=Provider.GITHUB,
        connection="c",
        namespace="forge-sdlc/forge",
        default_branch="main",
        change_request_mode="fork",
    )
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
        patch("forge.workflow.nodes.error_handler.get_adapter", return_value=(repo_ref, adapter)),
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
    adapter.create_comment.assert_awaited_once()
    call_args = adapter.create_comment.call_args[0]
    assert call_args[0] is repo_ref
    assert "vertex-ai: vertex=" in call_args[2]


@pytest.mark.asyncio
async def test_github_mirror_failure_is_swallowed() -> None:
    """A SourceControlError from get_adapter/create_comment doesn't fail notify_error."""
    jira = MagicMock()
    jira.get_issue = AsyncMock(return_value=MagicMock(reporter=None, assignee=None))
    jira.add_model_policy_error_comment = AsyncMock()
    jira.close = AsyncMock()
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
        patch(
            "forge.workflow.nodes.error_handler.get_adapter",
            side_effect=NotFoundError("no adapter registered"),
        ),
    ):
        await notify_error(state, error, "implement_task")

    jira.add_model_policy_error_comment.assert_awaited_once()
    jira.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_model_error_is_not_mirrored_to_github() -> None:
    jira = MagicMock()
    jira.get_issue = AsyncMock(return_value=MagicMock(reporter=None, assignee=None))
    jira.add_error_comment = AsyncMock()
    jira.close = AsyncMock()

    with (
        patch("forge.workflow.nodes.error_handler.JiraClient", return_value=jira),
        patch("forge.workflow.nodes.error_handler.get_adapter") as get_adapter_mock,
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

    get_adapter_mock.assert_not_called()
