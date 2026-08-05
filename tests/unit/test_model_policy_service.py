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
        await resolve_model_target_for_project(settings, "PROJ", "arbitrary-legacy-task", jira=jira)
        is None
    )
    jira.get_project_property.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_only_policy_does_not_fetch_jira() -> None:
    settings = MagicMock(has_explicit_model_policy=True, model_connections={})
    resolver = MagicMock()
    expected = MagicMock()
    resolver.resolve.return_value = expected
    settings.model_policy_resolver.return_value = resolver
    jira = MagicMock()
    jira.get_project_property = AsyncMock()

    result = await resolve_model_target_for_project(settings, "PROJ", "generate_prd", jira=jira)

    assert result is expected
    resolver.resolve.assert_called_once_with("generate_prd", {}, None)
    jira.get_project_property.assert_not_awaited()


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
    resolver.resolve.assert_called_once_with("generate_prd", {}, None)


@pytest.mark.asyncio
async def test_project_default_is_separate_fallback_for_stage_policy() -> None:
    settings = MagicMock(
        has_explicit_model_policy=True,
        model_connections={"vertex": {}},
    )
    resolver = MagicMock()
    expected = MagicMock()
    resolver.resolve.return_value = expected
    settings.model_policy_resolver.return_value = resolver
    project_policy = {"generate_prd": {"connection": "vertex", "model": "gemini-pro"}}
    project_default = {"connection": "vertex", "model": "gemini-flash"}
    jira = MagicMock()
    jira.get_project_property = AsyncMock(
        side_effect=lambda _project, prop: {
            "forge.model_policy": project_policy,
            "forge.model_default": project_default,
        }[prop]
    )

    result = await resolve_model_target_for_project(settings, "PROJ", "generate_spec", jira=jira)

    assert result is expected
    resolver.resolve.assert_called_once_with("generate_spec", project_policy, project_default)


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


@pytest.mark.asyncio
async def test_malformed_project_default_fails_closed() -> None:
    settings = MagicMock(
        has_explicit_model_policy=True,
        model_connections={"vertex": {}},
    )
    jira = MagicMock()
    jira.get_project_property = AsyncMock(side_effect=[{}, []])

    with pytest.raises(ValueError, match="forge.model_default.*must be an object"):
        await resolve_model_target_for_project(settings, "PROJ", "generate_prd", jira=jira)
