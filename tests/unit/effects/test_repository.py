from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.domain import EffectCommand, ResourceIdentity, WorkflowIdentity
from forge.effects.repository import REPOSITORY_PUSH_OPERATION, RepositoryPushExecutor


def _command() -> EffectCommand:
    return EffectCommand(
        effect_id="push-1",
        idempotency_key="push-1",
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        operation=REPOSITORY_PUSH_OPERATION,
        target=ResourceIdentity(
            resource_type="repository_ref", external_id="forge/forge-1", namespace="org/repo"
        ),
        payload={
            "workspace_path": "/tmp/forge-test",
            "repository": "org/repo",
            "branch": "forge/forge-1",
            "ticket_key": "FORGE-1",
            "commit_sha": "abc123",
            "use_fork": True,
            "force": False,
            "check_conflicts": True,
        },
    )


@pytest.mark.asyncio
async def test_push_recovers_after_provider_success_without_pushing_again() -> None:
    adapter = MagicMock()
    adapter.get_git_credentials = AsyncMock(return_value=MagicMock())
    registry = MagicMock()
    registry.resolve.return_value = MagicMock(adapter=adapter, repo_ref=MagicMock())
    git = MagicMock()
    git.get_current_sha.return_value = "abc123"
    git.get_remote_branch_sha.return_value = "abc123"

    with patch("forge.effects.repository.GitOperations", return_value=git):
        result = await RepositoryPushExecutor(lambda: registry).execute(_command())

    git.push_to_fork.assert_not_called()
    assert result.provider_reference == "fork:forge/forge-1@abc123"


@pytest.mark.asyncio
async def test_push_updates_remote_when_commit_is_missing() -> None:
    adapter = MagicMock()
    adapter.get_git_credentials = AsyncMock(return_value=MagicMock())
    registry = MagicMock()
    registry.resolve.return_value = MagicMock(adapter=adapter, repo_ref=MagicMock())
    git = MagicMock()
    git.get_current_sha.return_value = "abc123"
    git.get_remote_branch_sha.return_value = None

    with patch("forge.effects.repository.GitOperations", return_value=git):
        await RepositoryPushExecutor(lambda: registry).execute(_command())

    git.push_to_fork.assert_called_once_with(force=False)


@pytest.mark.asyncio
async def test_stale_push_is_superseded_by_newer_local_commit() -> None:
    adapter = MagicMock()
    adapter.get_git_credentials = AsyncMock(return_value=MagicMock())
    registry = MagicMock()
    registry.resolve.return_value = MagicMock(adapter=adapter, repo_ref=MagicMock())
    git = MagicMock()
    git.get_current_sha.return_value = "newer456"

    with patch("forge.effects.repository.GitOperations", return_value=git):
        result = await RepositoryPushExecutor(lambda: registry).execute(_command())

    git.push_to_fork.assert_not_called()
    assert result.output == {"superseded_by": "newer456"}
