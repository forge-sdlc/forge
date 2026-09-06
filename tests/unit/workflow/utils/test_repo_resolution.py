"""Tests for workflow repository configuration authority."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.client import MissingProjectConfig
from forge.workflow.utils.repo_resolution import (
    ensure_repo_labels,
    get_effective_default_repo,
    get_effective_repos,
    reconcile_repo_labels,
    repos_from_labels,
)


@pytest.mark.asyncio
async def test_local_mode_prefers_environment_repos_over_jira() -> None:
    jira = AsyncMock()
    settings = MagicMock(
        forge_require_project_config=False,
        known_repos=["local/one", "local/two"],
        github_default_repo="local/two",
    )

    with patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings):
        assert await get_effective_repos(jira, "PROJ") == ["local/one", "local/two"]
        assert await get_effective_default_repo(jira, "PROJ") == "local/two"

    jira.get_project_repos.assert_not_awaited()
    jira.get_project_default_repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_production_mode_uses_only_jira_project_config() -> None:
    jira = AsyncMock()
    jira.get_project_repos.return_value = ["prod/repo"]
    jira.get_project_default_repo.return_value = "prod/repo"
    settings = MagicMock(
        forge_require_project_config=True,
        known_repos=["local/repo"],
        github_default_repo="local/repo",
    )

    with patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings):
        assert await get_effective_repos(jira, "PROJ") == ["prod/repo"]
        assert await get_effective_default_repo(jira, "PROJ") == "prod/repo"

    jira.get_project_repos.assert_awaited_once_with("PROJ")
    jira.get_project_default_repo.assert_awaited_once_with("PROJ")


@pytest.mark.asyncio
async def test_local_mode_does_not_fall_back_to_jira_when_env_is_missing() -> None:
    jira = AsyncMock()
    settings = MagicMock(
        forge_require_project_config=False,
        known_repos=[],
        github_default_repo="",
    )

    with (
        patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings),
        pytest.raises(MissingProjectConfig, match="GITHUB_KNOWN_REPOS"),
    ):
        await get_effective_repos(jira, "PROJ")

    jira.get_project_repos.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mode_missing_default_repo_does_not_fall_back_to_jira() -> None:
    jira = AsyncMock()
    settings = MagicMock(
        forge_require_project_config=False,
        known_repos=["local/repo"],
        github_default_repo="",
    )

    with (
        patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings),
        pytest.raises(MissingProjectConfig, match="GITHUB_DEFAULT_REPO"),
    ):
        await get_effective_default_repo(jira, "PROJ")

    jira.get_project_default_repo.assert_not_awaited()


def test_repos_from_labels_preserves_all_valid_repositories() -> None:
    assert repos_from_labels(
        ["repo:owner/one", "forge:managed", "repo:owner/two", "repo:owner/one"]
    ) == ["owner/one", "owner/two"]


@pytest.mark.asyncio
async def test_ensure_repo_labels_materializes_existing_resolution() -> None:
    jira = AsyncMock()
    jira.get_project_repos.return_value = ["owner/one", "owner/two"]
    issue = SimpleNamespace(
        key="PROJ-1",
        project_key="PROJ",
        summary="Change owner/two",
        description="",
        labels=["forge:managed"],
    )
    settings = MagicMock(forge_require_project_config=True)

    with patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings):
        resolved = await ensure_repo_labels(jira, issue, issue.summary)

    assert resolved == ["owner/two"]
    jira.add_labels.assert_awaited_once_with("PROJ-1", ["repo:owner/two"])


@pytest.mark.asyncio
async def test_ensure_repo_labels_adds_all_structured_plan_repositories() -> None:
    jira = AsyncMock()
    jira.get_project_repos.return_value = ["owner/one", "owner/two"]
    issue = SimpleNamespace(
        key="PROJ-1",
        project_key="PROJ",
        summary="Feature",
        description="",
        labels=["repo:owner/one"],
    )
    settings = MagicMock(forge_require_project_config=True)

    with patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings):
        resolved = await ensure_repo_labels(
            jira,
            issue,
            "Implement in owner/two",
            ["owner/two", "not-configured/repo"],
        )

    assert resolved == ["owner/two", "owner/one"]
    jira.add_labels.assert_awaited_once_with("PROJ-1", ["repo:owner/two"])


@pytest.mark.asyncio
async def test_ensure_repo_labels_passes_reconciliation_scope() -> None:
    jira = AsyncMock()
    jira.get_project_repos.return_value = ["owner/repo"]
    issue = SimpleNamespace(
        key="PROJ-1",
        project_key="PROJ",
        summary="Change owner/repo",
        description="",
        labels=["forge:managed"],
    )
    settings = MagicMock(forge_require_project_config=True)

    with patch("forge.workflow.utils.repo_resolution.get_settings", return_value=settings):
        await ensure_repo_labels(
            jira,
            issue,
            issue.summary,
            effect_scope="generate_spec",
        )

    jira.add_labels.assert_awaited_once_with(
        "PROJ-1",
        ["repo:owner/repo"],
        effect_scope="generate_spec",
    )


@pytest.mark.asyncio
async def test_reconcile_repo_labels_replaces_stale_label_without_readding_retained_label() -> None:
    jira = AsyncMock()
    jira.get_labels.return_value = ["forge:managed", "repo:owner/old", "repo:owner/two"]

    selected = await reconcile_repo_labels(jira, "PROJ-1", ["owner/two"])

    assert selected == ["owner/two"]
    jira.remove_labels.assert_awaited_once_with("PROJ-1", ["repo:owner/old"])
    jira.add_labels.assert_not_awaited()
