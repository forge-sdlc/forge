"""Integration tests for fork-based workflow.

These tests verify:
1. Fork creation and retrieval
2. Fork syncing with upstream
3. PR creation with fork head format
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.github.client import GitHubClient


class TestForkCreation:
    """Test fork creation and management."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.github_token.get_secret_value.return_value = "test-token"
        settings.github_fork_owner = ""  # Default to authenticated user
        return settings

    @pytest.fixture
    def github_client(self, mock_settings):
        """Create GitHub client with mock settings."""
        with patch("forge.integrations.github.client.get_settings", return_value=mock_settings):
            return GitHubClient(mock_settings)

    async def test_get_fork_returns_existing_fork(self, github_client):
        """Should return existing fork if it exists."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "full_name": "forge-user/test-repo",
            "fork": True,
            "parent": {
                "owner": {"login": "upstream-org"},
                "name": "test-repo",
            },
        }

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            # Mock authenticated user
            user_response = MagicMock()
            user_response.json.return_value = {"login": "forge-user"}
            mock_client.get.side_effect = [user_response, mock_response]

            result = await github_client.get_fork("upstream-org", "test-repo")

            assert result is not None
            assert result["full_name"] == "forge-user/test-repo"
            assert result["fork"] is True

    async def test_get_fork_returns_none_if_not_exists(self, github_client):
        """Should return None if fork doesn't exist."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_get_client.return_value = mock_client

            # Mock authenticated user
            user_response = MagicMock()
            user_response.json.return_value = {"login": "forge-user"}

            # Mock 404 for fork
            fork_response = MagicMock()
            fork_response.status_code = 404

            mock_client.get = AsyncMock(side_effect=[user_response, fork_response])

            result = await github_client.get_fork("upstream-org", "test-repo")

            assert result is None

    async def test_create_fork_calls_api_correctly(self, github_client):
        """Should call GitHub API to create fork."""
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {
            "full_name": "forge-user/test-repo",
            "owner": {"login": "forge-user"},
            "name": "test-repo",
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await github_client.create_fork("upstream-org", "test-repo")

            # Verify API call
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/repos/upstream-org/test-repo/forks" in call_args[0][0]

            assert result["full_name"] == "forge-user/test-repo"

    async def test_get_or_create_fork_uses_existing(self, github_client):
        """Should use existing fork if available."""
        existing_fork = {
            "full_name": "forge-user/test-repo",
            "owner": {"login": "forge-user"},
            "name": "test-repo",
            "fork": True,
            "parent": {
                "owner": {"login": "upstream-org"},
                "name": "test-repo",
            },
        }

        with patch.object(github_client, "get_fork", return_value=existing_fork):
            with patch.object(github_client, "create_fork") as mock_create:
                result = await github_client.get_or_create_fork(
                    "upstream-org", "test-repo", wait_for_ready=False
                )

                # Should not create new fork
                mock_create.assert_not_called()
                assert result["full_name"] == "forge-user/test-repo"

    async def test_get_or_create_fork_creates_if_missing(self, github_client):
        """Should create fork if none exists."""
        new_fork = {
            "full_name": "forge-user/test-repo",
            "owner": {"login": "forge-user"},
            "name": "test-repo",
            "default_branch": "main",
        }

        with patch.object(github_client, "get_fork", return_value=None):
            with patch.object(github_client, "create_fork", return_value=new_fork):
                result = await github_client.get_or_create_fork(
                    "upstream-org", "test-repo", wait_for_ready=False
                )

                assert result["full_name"] == "forge-user/test-repo"


class TestForkSync:
    """Test fork synchronization with upstream."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.github_token.get_secret_value.return_value = "test-token"
        settings.github_fork_owner = "forge-user"
        return settings

    @pytest.fixture
    def github_client(self, mock_settings):
        """Create GitHub client with mock settings."""
        with patch("forge.integrations.github.client.get_settings", return_value=mock_settings):
            return GitHubClient(mock_settings)

    async def test_sync_fork_success(self, github_client):
        """Should sync fork with upstream successfully."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "Successfully fetched and fast-forwarded from upstream",
        }

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await github_client.sync_fork_with_upstream("forge-user", "test-repo")

            assert result is True
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/repos/forge-user/test-repo/merge-upstream" in call_args[0][0]

    async def test_sync_fork_already_up_to_date(self, github_client):
        """Should handle fork already up to date."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "This branch is already up to date",
        }

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await github_client.sync_fork_with_upstream("forge-user", "test-repo")

            assert result is True

    async def test_sync_fork_conflict(self, github_client):
        """Should handle fork that has diverged."""
        mock_response = MagicMock()
        mock_response.status_code = 409

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await github_client.sync_fork_with_upstream("forge-user", "test-repo")

            assert result is False


class TestPRFromFork:
    """Test PR creation from fork."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.github_token.get_secret_value.return_value = "test-token"
        settings.github_fork_owner = "forge-user"
        return settings

    @pytest.fixture
    def github_client(self, mock_settings):
        """Create GitHub client with mock settings."""
        with patch("forge.integrations.github.client.get_settings", return_value=mock_settings):
            return GitHubClient(mock_settings)

    async def test_create_pr_with_fork_head_format(self, github_client):
        """Should create PR with fork:branch head format."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "number": 123,
            "html_url": "https://github.com/upstream-org/test-repo/pull/123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_client, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            # Create PR with fork head format
            result = await github_client.create_pull_request(
                owner="upstream-org",
                repo="test-repo",
                title="Test PR",
                body="Test body",
                head="forge-user:feature-branch",  # Fork head format
                base="main",
            )

            # Verify the API call used correct head format
            call_args = mock_client.post.call_args
            json_body = call_args[1]["json"]
            assert json_body["head"] == "forge-user:feature-branch"
            assert json_body["base"] == "main"

            assert result["number"] == 123

    async def test_get_fork_owner_uses_config(self, github_client):
        """Should use configured fork owner if set."""
        result = await github_client.get_fork_owner()
        assert result == "forge-user"

    async def test_get_fork_owner_falls_back_to_authenticated_user(self, mock_settings):
        """Should fall back to authenticated user if not configured."""
        mock_settings.github_fork_owner = ""  # Not configured

        with patch("forge.integrations.github.client.get_settings", return_value=mock_settings):
            client = GitHubClient(mock_settings)

            user_response = MagicMock()
            user_response.json.return_value = {"login": "authenticated-user"}
            user_response.raise_for_status = MagicMock()

            with patch.object(client, "_get_client") as mock_get_client:
                mock_http = AsyncMock()
                mock_http.get = AsyncMock(return_value=user_response)
                mock_get_client.return_value = mock_http

                result = await client.get_fork_owner()
                assert result == "authenticated-user"
