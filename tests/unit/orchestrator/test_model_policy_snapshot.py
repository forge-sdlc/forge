"""Focused tests for workflow model-policy checkpoint pinning."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.orchestrator.worker import OrchestratorWorker


@pytest.mark.asyncio
async def test_configured_policy_is_resolved_once_into_checkpoint_state() -> None:
    worker = object.__new__(OrchestratorWorker)
    resolver = MagicMock()
    resolver.resolve_all.return_value = {
        "generate_prd": {
            "connection": "vertex",
            "model": "gemini-pro",
            "backend": "vertex-ai",
            "policy_key": "generate_prd",
            "policy_source": "project",
            "project": "prod",
            "location": "global",
        }
    }
    worker.settings = MagicMock(
        model_connections={"vertex": {}},
        model_policy={},
        model_default={},
    )
    worker.settings.model_policy_resolver.return_value = resolver
    jira = MagicMock()
    jira.get_project_property = AsyncMock(
        return_value={"generate_prd": {"connection": "vertex", "model": "gemini-pro"}}
    )

    pinned = await worker._ensure_model_policy_snapshot({}, "PROJ-123", jira)
    pinned_again = await worker._ensure_model_policy_snapshot(pinned, "PROJ-123", jira)

    assert pinned["model_policy_snapshot"] == resolver.resolve_all.return_value
    assert pinned_again is pinned
    jira.get_project_property.assert_awaited_once_with("PROJ", "forge.model_policy")


@pytest.mark.asyncio
async def test_legacy_configuration_does_not_add_snapshot() -> None:
    worker = object.__new__(OrchestratorWorker)
    worker.settings = MagicMock(model_connections={}, model_policy={}, model_default={})
    jira = MagicMock()
    jira.get_project_property = AsyncMock()
    state = {"ticket_key": "PROJ-123"}

    result = await worker._ensure_model_policy_snapshot(state, "PROJ-123", jira)

    assert result is state
    jira.get_project_property.assert_not_awaited()
