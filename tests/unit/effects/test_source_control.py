from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.domain import EffectCommand, ResourceIdentity, WorkflowIdentity
from forge.effects.source_control import (
    SC_BRANCH_CREATE_OPERATION,
    SC_CHANGE_REQUEST_UPDATE_OPERATION,
    SC_COMMENT_CREATE_OPERATION,
    SC_COMMENT_REPLY_OPERATION,
    SC_FILE_PUT_OPERATION,
    SourceControlMutationExecutor,
)
from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    Provider,
    RepositoryRef,
    ResolvedRepository,
    Review,
    ReviewComment,
)


def _fixture(operation: str, payload: dict) -> tuple[EffectCommand, MagicMock, MagicMock]:
    repo = RepositoryRef(
        id="repo-1",
        provider=Provider.GITHUB,
        connection="github",
        namespace="org/repo",
        default_branch="main",
        change_request_mode="direct",
    )
    adapter = MagicMock()
    registry = MagicMock()
    registry.resolve.return_value = ResolvedRepository(
        repo_ref=repo, connection=MagicMock(), adapter=adapter
    )
    command = EffectCommand(
        effect_id="effect-1",
        idempotency_key="stable-key",
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        operation=operation,
        target=ResourceIdentity(
            resource_type="change_request", external_id="17", namespace="org/repo"
        ),
        payload=payload,
    )
    return command, registry, adapter


@pytest.mark.asyncio
async def test_comment_effect_recovers_from_provider_marker() -> None:
    command, registry, adapter = _fixture(SC_COMMENT_CREATE_OPERATION, {"body": "Done"})
    adapter.get_change_request_comments = AsyncMock(
        return_value=[ReviewComment(id="9", body="<!-- forge-effect:stable-key -->", author="bot")]
    )
    adapter.create_comment = AsyncMock()

    result = await SourceControlMutationExecutor(
        SC_COMMENT_CREATE_OPERATION, lambda: registry
    ).execute(command)

    adapter.create_comment.assert_not_awaited()
    assert result.provider_reference == "9"


@pytest.mark.asyncio
async def test_comment_effect_leaves_recovery_marker() -> None:
    command, registry, adapter = _fixture(SC_COMMENT_CREATE_OPERATION, {"body": "Done"})
    adapter.get_change_request_comments = AsyncMock(return_value=[])
    adapter.create_comment = AsyncMock(return_value=ReviewComment(id="10", body="", author="bot"))

    await SourceControlMutationExecutor(SC_COMMENT_CREATE_OPERATION, lambda: registry).execute(
        command
    )

    assert "forge-effect:stable-key" in adapter.create_comment.await_args.args[2]


@pytest.mark.asyncio
async def test_branch_and_change_request_mutations_use_provider_contract() -> None:
    command, registry, adapter = _fixture(
        SC_BRANCH_CREATE_OPERATION, {"name": "forge/work", "base": "main"}
    )
    adapter.create_branch = AsyncMock()
    result = await SourceControlMutationExecutor(
        SC_BRANCH_CREATE_OPERATION, lambda: registry
    ).execute(command)
    adapter.create_branch.assert_awaited_once()
    assert result.provider_reference == "forge/work"

    command, registry, adapter = _fixture(
        SC_CHANGE_REQUEST_UPDATE_OPERATION, {"body": "updated", "state": "open"}
    )
    adapter.update_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity("github", "repo-1", 17),
            url="https://example.test/17",
            title="PR",
            body="updated",
            state=ChangeRequestState.OPEN,
            source_branch="work",
            target_branch="main",
        )
    )
    result = await SourceControlMutationExecutor(
        SC_CHANGE_REQUEST_UPDATE_OPERATION, lambda: registry
    ).execute(command)
    assert result.output["number"] == "17"


@pytest.mark.asyncio
async def test_file_effect_recovers_after_provider_success_before_acknowledgement() -> None:
    command, registry, adapter = _fixture(
        SC_FILE_PUT_OPERATION,
        {
            "path": "docs/plan.md",
            "content": "same content",
            "message": "Publish plan",
            "branch": "forge/work",
        },
    )
    adapter.get_file = AsyncMock(return_value="same content")
    adapter.put_file = AsyncMock()

    result = await SourceControlMutationExecutor(
        SC_FILE_PUT_OPERATION, lambda: registry
    ).execute(command)

    adapter.put_file.assert_not_awaited()
    assert result.provider_reference == "forge/work:docs/plan.md"


@pytest.mark.asyncio
async def test_review_reply_recovers_from_inline_thread_marker() -> None:
    command, registry, adapter = _fixture(
        SC_COMMENT_REPLY_OPERATION, {"body": "Fixed", "comment_id": "8"}
    )
    adapter.get_review_thread_comments = AsyncMock(
        return_value=[
            Review(
                id="thread-1",
                state="commented",
                body="",
                author="reviewer",
                comments=[
                    ReviewComment(
                        id="9", body="<!-- forge-effect:stable-key -->", author="bot"
                    )
                ],
            )
        ]
    )
    adapter.reply_to_comment = AsyncMock()

    result = await SourceControlMutationExecutor(
        SC_COMMENT_REPLY_OPERATION, lambda: registry
    ).execute(command)

    adapter.reply_to_comment.assert_not_awaited()
    assert result.provider_reference == "9"
