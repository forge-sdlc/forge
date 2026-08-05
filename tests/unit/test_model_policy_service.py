"""Tests for shared live Jira model-policy resolution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.model_policy import resolve_model_target_for_project


@pytest.mark.asyncio
async def test_legacy_configuration_preserves_existing_model_paths() -> None:
    settings = MagicMock(has_explicit_model_policy=False)
    assert await resolve_model_target_for_project(settings, "PROJ", "implement_task") is None


@pytest.mark.asyncio
async def test_missing_project_policy_uses_global_mapping() -> None:
    settings = MagicMock(
        has_explicit_model_policy=True,
        model_connections={"vertex": {}},
    )
    resolver = MagicMock()
    expected = MagicMock()
    resolver.resolve.return_value = expected
    settings.model_policy_resolver.return_value = resolver
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=None)

    result = await resolve_model_target_for_project(settings, "PROJ", "generate-prd", jira=jira)

    assert result is expected
    resolver.resolve.assert_called_once_with("generate_prd", {})


@pytest.mark.asyncio
async def test_malformed_project_policy_fails_closed() -> None:
    settings = MagicMock(
        has_explicit_model_policy=True,
        model_connections={"vertex": {}},
    )
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="must be an object"):
        await resolve_model_target_for_project(settings, "PROJ", "generate_prd", jira=jira)
