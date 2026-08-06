"""External clients reject generated secrets before making HTTP requests."""

from unittest.mock import AsyncMock

import pytest

from forge.config import Settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.security.secrets import SecretDetectedError

SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.mark.asyncio
async def test_github_pr_body_is_scanned_before_http(mock_settings: Settings) -> None:
    github = GitHubClient(mock_settings)
    github._get_client = AsyncMock()

    with pytest.raises(SecretDetectedError):
        await github.create_pull_request(
            "owner", "repo", "title", f"aws_access_key_id={SECRET}", "branch"
        )

    github._get_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_github_comment_is_scanned_before_http(mock_settings: Settings) -> None:
    github = GitHubClient(mock_settings)
    github._get_client = AsyncMock()

    with pytest.raises(SecretDetectedError):
        await github.create_issue_comment("owner", "repo", 1, f"aws_access_key_id={SECRET}")

    github._get_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_jira_comment_is_scanned_before_http(mock_settings: Settings) -> None:
    jira = JiraClient(mock_settings)
    jira._get_client = AsyncMock()

    with pytest.raises(SecretDetectedError):
        await jira.add_comment("FORGE-1", f"aws_access_key_id={SECRET}")

    jira._get_client.assert_not_awaited()
