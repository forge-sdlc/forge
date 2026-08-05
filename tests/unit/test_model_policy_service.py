"""Tests for shared live Jira model-policy resolution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.model_policy import resolve_model_target_for_project


@pytest.mark.asyncio
async def test_legacy_configuration_preserves_existing_model_paths() -> None:
    settings = MagicMock(has_explicit_model_policy=False)
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=None)

    assert (
        await resolve_model_target_for_project(settings, "PROJ", "implement_task", jira=jira)
        is None
    )
    jira.get_project_property.assert_awaited_once_with("PROJ", "forge.model_policy")


@pytest.mark.asyncio
async def test_legacy_environment_applies_live_project_policy() -> None:
    settings = MagicMock(has_explicit_model_policy=False)
    resolver = MagicMock()
    expected = MagicMock()
    resolver.resolve.return_value = expected
    settings.model_policy_resolver.return_value = resolver
    project_policy = {"generate_prd": {"connection": "default", "model": "gemini-3.5-pro"}}
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=project_policy)

    result = await resolve_model_target_for_project(settings, "PROJ", "generate_prd", jira=jira)

    assert result is expected
    resolver.resolve.assert_called_once_with("generate_prd", project_policy)


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
