from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.domain import EffectCommand, ResourceIdentity, WorkflowIdentity
from forge.effects.jira import (
    JIRA_ATTACHMENT_REPLACE_OPERATION,
    JIRA_COMMENT_OPERATION,
    JIRA_CUSTOM_FIELD_OPERATION,
    JIRA_DESCRIPTION_OPERATION,
    JIRA_LABEL_OPERATION,
    JIRA_LABELS_ADD_OPERATION,
    JIRA_TRANSITION_OPERATION,
    JiraCommentExecutor,
    JiraMutationExecutor,
)


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


@pytest.mark.parametrize(
    ("operation", "payload", "method", "expected"),
    [
        (
            JIRA_LABEL_OPERATION,
            {"label": "forge:done"},
            "set_workflow_label",
            ("FORGE-1", "forge:done"),
        ),
        (
            JIRA_DESCRIPTION_OPERATION,
            {"description": "new"},
            "update_description",
            ("FORGE-1", "new"),
        ),
        (
            JIRA_CUSTOM_FIELD_OPERATION,
            {"field": "customfield_1", "value": "new"},
            "update_custom_field",
            ("FORGE-1", "customfield_1", "new"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_idempotent_jira_mutation_executors(operation, payload, method, expected) -> None:
    jira = MagicMock()
    setattr(jira, method, AsyncMock())
    jira.close = AsyncMock()
    command = _command().model_copy(update={"operation": operation, "payload": payload})

    result = await JiraMutationExecutor(operation, lambda: jira).execute(command)

    getattr(jira, method).assert_awaited_once_with(*expected)
    assert result.provider_reference == "FORGE-1"
    jira.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_attachment_replace_recovers_by_replacing_name() -> None:
    jira = MagicMock()
    jira.delete_attachments_by_name = AsyncMock(return_value=1)
    jira.add_attachment = AsyncMock(return_value=SimpleNamespace(id="attachment-2"))
    jira.close = AsyncMock()
    command = _command().model_copy(
        update={
            "operation": JIRA_ATTACHMENT_REPLACE_OPERATION,
            "payload": {
                "filename": "spec.md",
                "content": "body",
                "content_type": "text/markdown",
            },
        }
    )

    result = await JiraMutationExecutor(JIRA_ATTACHMENT_REPLACE_OPERATION, lambda: jira).execute(
        command
    )

    jira.delete_attachments_by_name.assert_awaited_once_with("FORGE-1", "spec.md")
    jira.add_attachment.assert_awaited_once_with(
        "FORGE-1",
        filename="spec.md",
        content="body",
        content_type="text/markdown",
    )
    assert result.provider_reference == "attachment-2"
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


@pytest.mark.asyncio
async def test_transition_recovers_when_target_status_was_already_reached() -> None:
    jira = MagicMock()
    jira.get_issue = AsyncMock(return_value=SimpleNamespace(status="Closed"))
    jira.transition_issue = AsyncMock()
    jira.close = AsyncMock()
    command = _command().model_copy(
        update={"operation": JIRA_TRANSITION_OPERATION, "payload": {"transition": "Closed"}}
    )

    await JiraMutationExecutor(JIRA_TRANSITION_OPERATION, lambda: jira).execute(command)

    jira.transition_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_labels_only_writes_missing_values() -> None:
    jira = MagicMock()
    jira.get_labels = AsyncMock(return_value=["existing"])
    jira.add_labels = AsyncMock()
    jira.close = AsyncMock()
    command = _command().model_copy(
        update={
            "operation": JIRA_LABELS_ADD_OPERATION,
            "payload": {"labels": ["existing", "new"]},
        }
    )

    await JiraMutationExecutor(JIRA_LABELS_ADD_OPERATION, lambda: jira).execute(command)

    jira.add_labels.assert_awaited_once_with("FORGE-1", ["new"])
