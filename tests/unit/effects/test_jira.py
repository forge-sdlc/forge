from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.domain import EffectCommand, ResourceIdentity, WorkflowIdentity
from forge.effects.jira import JIRA_COMMENT_OPERATION, JiraCommentExecutor


def _command() -> EffectCommand:
    return EffectCommand(
        effect_id="effect-1",
        idempotency_key="stable-key",
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        operation=JIRA_COMMENT_OPERATION,
        target=ResourceIdentity(resource_type="issue", external_id="FORGE-1"),
        payload={"body": "Work accepted"},
    )


@pytest.mark.asyncio
async def test_comment_executor_adds_recovery_marker() -> None:
    jira = MagicMock()
    jira.get_comments = AsyncMock(return_value=[])
    jira.add_comment = AsyncMock(return_value=SimpleNamespace(id="comment-1"))
    jira.close = AsyncMock()

    result = await JiraCommentExecutor(lambda: jira).execute(_command())

    body = jira.add_comment.await_args.args[1]
    assert "forge-effect:stable-key" in body
    assert result.provider_reference == "comment-1"
    jira.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_after_crash_finds_provider_marker_without_duplicate() -> None:
    jira = MagicMock()
    jira.get_comments = AsyncMock(
        return_value=[SimpleNamespace(id="comment-1", body="{forge-effect:stable-key}")]
    )
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    result = await JiraCommentExecutor(lambda: jira).execute(_command())

    jira.add_comment.assert_not_awaited()
    assert result.provider_reference == "comment-1"
