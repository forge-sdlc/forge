"""Unit tests for post_merge_summary node."""

from unittest.mock import AsyncMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.nodes.post_merge_summary import post_merge_summary


@pytest.fixture
def state_post_merge():
    return {
        "ticket_key": "BUG-42",
        "ticket_type": TicketType.BUG,
        "current_node": "human_review_gate",
        "is_paused": False,
        "rca_content": "Password regex excludes special characters.",
        "plan_content": "## Plan\n\nFix regex in validators.py.",
        "selected_fix_approach": {
            "title": "Fix regex",
            "description": "Update VALID_PASSWORD_PATTERN.",
            "tradeoffs": "Low risk.",
        },
        "pr_urls": ["https://github.com/acme/backend/pull/99"],
        "current_repo": "acme/backend",
        "retry_count": 0,
        "last_error": None,
    }


def _make_mock_jira():
    jira = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


class TestPostMergeSummary:
    @pytest.mark.asyncio
    async def test_posts_jira_comment(self, state_post_merge):
        """Posts a Jira comment with fix summary and release note block."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            await post_merge_summary(state_post_merge)

        mock_jira.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_comment_contains_rca_content(self, state_post_merge):
        """Fix summary contains RCA-derived content."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            await post_merge_summary(state_post_merge)

        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "regex" in comment_text.lower() or "Password" in comment_text

    @pytest.mark.asyncio
    async def test_comment_contains_release_note(self, state_post_merge):
        """Comment includes a release note section."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            await post_merge_summary(state_post_merge)

        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "Release Note" in comment_text or "release note" in comment_text.lower()

    @pytest.mark.asyncio
    async def test_exception_is_non_blocking(self, state_post_merge):
        """Exception during comment post → logged, node returns normally without escalation."""
        mock_jira = _make_mock_jira()
        mock_jira.add_comment = AsyncMock(side_effect=RuntimeError("Jira unavailable"))

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            result = await post_merge_summary(state_post_merge)

        # Should return normally — no escalation
        assert result is not None
        assert result.get("current_node") != "escalate_blocked"

    @pytest.mark.asyncio
    async def test_returns_state_unchanged_on_success(self, state_post_merge):
        """On success, state is returned (current_node not changed)."""
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            result = await post_merge_summary(state_post_merge)

        assert result["ticket_key"] == "BUG-42"

    @pytest.mark.asyncio
    async def test_does_not_change_current_node(self, state_post_merge):
        """post_merge_summary does not set current_node — caller routes to END."""
        state_post_merge["current_node"] = "human_review_gate"
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            result = await post_merge_summary(state_post_merge)

        assert result.get("current_node") == "human_review_gate"

    @pytest.mark.asyncio
    async def test_impact_derived_from_rca(self, state_post_merge):
        """Impact in release note is derived from RCA content, not hardcoded."""
        state_post_merge["rca_content"] = "Login fails when password contains special characters."
        mock_jira = _make_mock_jira()

        with patch("forge.workflow.nodes.post_merge_summary.JiraClient", return_value=mock_jira):
            await post_merge_summary(state_post_merge)

        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "Login fails when password contains special characters" in comment_text
