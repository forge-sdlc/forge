"""Tests for GitHub Content API methods."""

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.integrations.github.client import GitHubClient


@pytest.fixture
def github_client(mock_settings):
    client = GitHubClient(settings=mock_settings)
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    return client


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_creates_branch_from_main(self, github_client):
        mock_client = github_client._client

        ref_response = MagicMock()
        ref_response.json.return_value = {"object": {"sha": "abc123"}}
        ref_response.raise_for_status = MagicMock()

        create_response = MagicMock()
        create_response.json.return_value = {"ref": "refs/heads/forge/prd/test-123"}
        create_response.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=ref_response)
        mock_client.post = AsyncMock(return_value=create_response)

        result = await github_client.create_branch("owner", "repo", "forge/prd/test-123")

        mock_client.get.assert_called_once_with("/repos/owner/repo/git/ref/heads/main")
        mock_client.post.assert_called_once_with(
            "/repos/owner/repo/git/refs",
            json={"ref": "refs/heads/forge/prd/test-123", "sha": "abc123"},
        )
        assert result["ref"] == "refs/heads/forge/prd/test-123"

    @pytest.mark.asyncio
    async def test_handles_branch_already_exists(self, github_client):
        mock_client = github_client._client

        ref_response = MagicMock()
        ref_response.json.return_value = {"object": {"sha": "abc123"}}
        ref_response.raise_for_status = MagicMock()

        error_response = MagicMock()
        error_response.status_code = 422
        error_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Unprocessable", request=MagicMock(), response=error_response
            )
        )

        mock_client.get = AsyncMock(return_value=ref_response)
        mock_client.post = AsyncMock(return_value=error_response)

        result = await github_client.create_branch("owner", "repo", "forge/prd/test-123")
        assert result is not None

    @pytest.mark.asyncio
    async def test_creates_branch_from_custom_base(self, github_client):
        mock_client = github_client._client

        ref_response = MagicMock()
        ref_response.json.return_value = {"object": {"sha": "def456"}}
        ref_response.raise_for_status = MagicMock()

        create_response = MagicMock()
        create_response.json.return_value = {"ref": "refs/heads/my-branch"}
        create_response.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=ref_response)
        mock_client.post = AsyncMock(return_value=create_response)

        result = await github_client.create_branch("owner", "repo", "my-branch", base="develop")

        mock_client.get.assert_called_once_with("/repos/owner/repo/git/ref/heads/develop")
        assert result["ref"] == "refs/heads/my-branch"


class TestCreateOrUpdateFile:
    @pytest.mark.asyncio
    async def test_creates_new_file(self, github_client):
        mock_client = github_client._client

        response = MagicMock()
        response.json.return_value = {
            "content": {"sha": "newsha123", "path": "proposals/TEST-123-my-feature.md"}
        }
        response.raise_for_status = MagicMock()
        mock_client.put = AsyncMock(return_value=response)

        result = await github_client.create_or_update_file(
            owner="owner",
            repo="repo",
            path="proposals/TEST-123-my-feature.md",
            content="# PRD content",
            message="Add PRD for TEST-123",
            branch="forge/prd/test-123",
        )

        call_args = mock_client.put.call_args
        assert call_args[0][0] == "/repos/owner/repo/contents/proposals/TEST-123-my-feature.md"
        body = call_args[1]["json"]
        assert body["branch"] == "forge/prd/test-123"
        assert body["message"] == "Add PRD for TEST-123"
        assert base64.b64decode(body["content"]).decode() == "# PRD content"
        assert "sha" not in body

    @pytest.mark.asyncio
    async def test_updates_existing_file_with_sha(self, github_client):
        mock_client = github_client._client

        response = MagicMock()
        response.json.return_value = {
            "content": {"sha": "updatedsha", "path": "proposals/TEST-123-my-feature.md"}
        }
        response.raise_for_status = MagicMock()
        mock_client.put = AsyncMock(return_value=response)

        await github_client.create_or_update_file(
            owner="owner",
            repo="repo",
            path="proposals/TEST-123-my-feature.md",
            content="# Updated PRD",
            message="Update PRD for TEST-123",
            branch="forge/prd/test-123",
            sha="oldsha456",
        )

        body = mock_client.put.call_args[1]["json"]
        assert body["sha"] == "oldsha456"


class TestGetFileContents:
    @pytest.mark.asyncio
    async def test_returns_file_metadata(self, github_client):
        mock_client = github_client._client

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "sha": "filesha789",
            "path": "proposals/TEST-123-my-feature.md",
            "content": base64.b64encode(b"# PRD").decode(),
        }
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        result = await github_client.get_file_contents(
            "owner", "repo", "proposals/TEST-123-my-feature.md", "forge/prd/test-123"
        )

        assert result["sha"] == "filesha789"
        mock_client.get.assert_called_once_with(
            "/repos/owner/repo/contents/proposals/TEST-123-my-feature.md",
            params={"ref": "forge/prd/test-123"},
        )

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, github_client):
        mock_client = github_client._client

        response = MagicMock()
        response.status_code = 404
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=response
            )
        )
        mock_client.get = AsyncMock(return_value=response)

        result = await github_client.get_file_contents(
            "owner", "repo", "proposals/nonexistent.md", "main"
        )

        assert result is None
