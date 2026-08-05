"""Unit tests for get_project_docs_repo method."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.client import JiraClient


@pytest.fixture
def jira_client():
    with patch("forge.integrations.jira.client.get_settings") as mock_settings:
        mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
        mock_settings.return_value.jira_api_token = MagicMock()
        mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
        mock_settings.return_value.jira_user_email = "test@example.com"
        client = JiraClient()
        client._project_property_cache = {}
        yield client


class TestGetProjectDocsRepo:
    """Tests for get_project_docs_repo method."""

    @pytest.mark.asyncio
    async def test_returns_repo_when_set(self, jira_client):
        """Returns repo string when forge.docs_repo is set."""
        jira_client.get_project_property = AsyncMock(return_value="acme/docs")

        result = await jira_client.get_project_docs_repo("MYPROJ")

        assert result == "acme/docs"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_set(self, jira_client):
        """Returns None when forge.docs_repo is not set."""
        jira_client.get_project_property = AsyncMock(return_value=None)

        result = await jira_client.get_project_docs_repo("MYPROJ")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_malformed_not_string(self, jira_client):
        """Returns None when forge.docs_repo is not a string."""
        jira_client.get_project_property = AsyncMock(return_value=["acme/docs"])

        result = await jira_client.get_project_docs_repo("MYPROJ")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_malformed_no_slash(self, jira_client):
        """Returns None when forge.docs_repo lacks owner/repo format."""
        jira_client.get_project_property = AsyncMock(return_value="docs-only")

        result = await jira_client.get_project_docs_repo("MYPROJ")

        assert result is None

    @pytest.mark.asyncio
    async def test_calls_get_project_property_with_correct_key(self, jira_client):
        """Calls get_project_property with forge.docs_repo key."""
        jira_client.get_project_property = AsyncMock(return_value=None)

        await jira_client.get_project_docs_repo("MYPROJ")

        jira_client.get_project_property.assert_called_once_with("MYPROJ", "forge.docs_repo")
