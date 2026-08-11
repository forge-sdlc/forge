"""Tests for GitHub outbound comment signature/prefix integration."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.config import Settings
from forge.integrations.github.client import GitHubClient


@pytest.fixture
def github_client(mock_settings: Settings) -> GitHubClient:
    # Set the default prefix to empty first
    mock_settings.forge_bot_comment_prefix = ""
    client = GitHubClient(settings=mock_settings)
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    return client


class TestGitHubOutboundCommentSigning:
    @pytest.mark.asyncio
    async def test_create_review_comment_with_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 1. Enable setting
        mock_settings.forge_bot_comment_prefix = "ForgeBotSignature"

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 123}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.create_review_comment(
            owner="owner",
            repo="repo",
            pr_number=45,
            body="Nice change!",
            commit_id="abc123",
            path="main.py",
            line=10,
        )

        assert result == {"id": 123}
        github_client._client.post.assert_called_once()
        call_args = github_client._client.post.call_args
        assert call_args[0][0] == "/repos/owner/repo/pulls/45/comments"

        # Verify body contains the prefix wrapped correctly
        body_sent = call_args[1]["json"]["body"]
        assert "<!-- ForgeBotSignature -->" in body_sent
        assert "Nice change!" in body_sent

    @pytest.mark.asyncio
    async def test_create_review_comment_no_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 2. Disable setting (empty)
        mock_settings.forge_bot_comment_prefix = ""

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 123}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.create_review_comment(
            owner="owner",
            repo="repo",
            pr_number=45,
            body="Nice change!",
            commit_id="abc123",
            path="main.py",
            line=10,
        )

        assert result == {"id": 123}
        call_args = github_client._client.post.call_args
        body_sent = call_args[1]["json"]["body"]
        assert body_sent == "Nice change!"

    @pytest.mark.asyncio
    async def test_create_issue_comment_with_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 1. Enable setting
        mock_settings.forge_bot_comment_prefix = "ForgeBotSignature"

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 456}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.create_issue_comment(
            owner="owner",
            repo="repo",
            issue_number=12,
            body="An issue comment.",
        )

        assert result == {"id": 456}
        github_client._client.post.assert_called_once()
        call_args = github_client._client.post.call_args
        assert call_args[0][0] == "/repos/owner/repo/issues/12/comments"

        # Verify body contains the prefix wrapped correctly
        body_sent = call_args[1]["json"]["body"]
        assert "<!-- ForgeBotSignature -->" in body_sent
        assert "An issue comment." in body_sent

    @pytest.mark.asyncio
    async def test_create_issue_comment_no_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 2. Disable setting (empty)
        mock_settings.forge_bot_comment_prefix = ""

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 456}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.create_issue_comment(
            owner="owner",
            repo="repo",
            issue_number=12,
            body="An issue comment.",
        )

        assert result == {"id": 456}
        call_args = github_client._client.post.call_args
        body_sent = call_args[1]["json"]["body"]
        assert body_sent == "An issue comment."

    @pytest.mark.asyncio
    async def test_reply_to_review_comment_with_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 1. Enable setting
        mock_settings.forge_bot_comment_prefix = "ForgeBotSignature"

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 789}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.reply_to_review_comment(
            owner="owner",
            repo="repo",
            pr_number=9,
            comment_id=77,
            body="Addressing.",
        )

        assert result == {"id": 789}
        github_client._client.post.assert_called_once()
        call_args = github_client._client.post.call_args
        assert call_args[0][0] == "/repos/owner/repo/pulls/9/comments/77/replies"

        # Verify body contains the prefix wrapped correctly
        body_sent = call_args[1]["json"]["body"]
        assert "<!-- ForgeBotSignature -->" in body_sent
        assert "Addressing." in body_sent

    @pytest.mark.asyncio
    async def test_reply_to_review_comment_no_prefix(
        self, github_client: Any, mock_settings: Any
    ) -> None:
        # 2. Disable setting (empty)
        mock_settings.forge_bot_comment_prefix = ""

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 789}
        mock_response.raise_for_status = MagicMock()
        github_client._client.post = AsyncMock(return_value=mock_response)

        result = await github_client.reply_to_review_comment(
            owner="owner",
            repo="repo",
            pr_number=9,
            comment_id=77,
            body="Addressing.",
        )

        assert result == {"id": 789}
        call_args = github_client._client.post.call_args
        body_sent = call_args[1]["json"]["body"]
        assert body_sent == "Addressing."
