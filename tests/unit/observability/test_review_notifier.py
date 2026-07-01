"""Unit tests for the ReviewJiraNotifier class."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.observability.review_notifier import (
    DEFAULT_RATE_LIMIT_SECONDS,
    MAX_FEEDBACK_LENGTH,
    NotifyResult,
    ReviewJiraNotifier,
)
from forge.observability.review_poller import ReviewCycleData

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_jira_client():
    """Create a mock JiraClient."""
    client = MagicMock()
    client.add_comment = AsyncMock()
    return client


@pytest.fixture
def review_cycle_data():
    """Create sample ReviewCycleData."""
    return ReviewCycleData(
        cycle=1,
        max_cycles=3,
        verdict="approved",
        feedback="The code looks good. All tests pass.",
        skill="local-code-review",
        elapsed_seconds=45.5,
        timestamp="2024-01-15T10:30:00Z",
    )


@pytest.fixture
def notifier(mock_jira_client):
    """Create a ReviewJiraNotifier with default settings."""
    return ReviewJiraNotifier(
        jira_client=mock_jira_client,
        ticket_key="TEST-123",
    )


# ---------------------------------------------------------------------------
# NotifyResult tests
# ---------------------------------------------------------------------------


class TestNotifyResult:
    """Tests for NotifyResult dataclass."""

    def test_posted_success(self):
        """Test successful post result."""
        result = NotifyResult(posted=True, rate_limited=False)
        assert result.posted is True
        assert result.rate_limited is False
        assert result.error is None

    def test_rate_limited(self):
        """Test rate limited result."""
        result = NotifyResult(posted=False, rate_limited=True)
        assert result.posted is False
        assert result.rate_limited is True
        assert result.error is None

    def test_error_result(self):
        """Test error result."""
        result = NotifyResult(posted=False, rate_limited=False, error="API Error")
        assert result.posted is False
        assert result.rate_limited is False
        assert result.error == "API Error"


# ---------------------------------------------------------------------------
# ReviewJiraNotifier initialization tests
# ---------------------------------------------------------------------------


class TestReviewJiraNotifierInit:
    """Tests for ReviewJiraNotifier initialization."""

    def test_init_basic(self, mock_jira_client):
        """Test basic initialization with defaults."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="PROJ-456",
        )
        assert notifier.jira_client is mock_jira_client
        assert notifier.ticket_key == "PROJ-456"
        assert notifier.rate_limit_seconds == DEFAULT_RATE_LIMIT_SECONDS
        assert notifier._last_notify_time is None

    def test_init_custom_rate_limit(self, mock_jira_client):
        """Test initialization with custom rate limit."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=60.0,
        )
        assert notifier.rate_limit_seconds == 60.0

    def test_default_rate_limit_is_30_seconds(self):
        """Test that the default rate limit is 30 seconds."""
        assert DEFAULT_RATE_LIMIT_SECONDS == 30.0


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for rate limiting functionality."""

    def test_not_rate_limited_initially(self, notifier):
        """Test that first call is not rate limited."""
        assert notifier._is_rate_limited() is False

    def test_rate_limited_after_notify(self, notifier):
        """Test that calls are rate limited after a notify."""
        # Simulate a recent notify by setting the timestamp
        notifier._last_notify_time = time.monotonic()
        assert notifier._is_rate_limited() is True

    def test_not_rate_limited_after_interval(self, mock_jira_client):
        """Test that calls are not rate limited after interval passes."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=0.1,  # Very short for testing
        )

        # Set a timestamp in the past
        notifier._last_notify_time = time.monotonic() - 0.2
        assert notifier._is_rate_limited() is False

    def test_reset_rate_limit(self, notifier):
        """Test that reset_rate_limit clears the timer."""
        notifier._last_notify_time = time.monotonic()
        assert notifier._is_rate_limited() is True

        notifier.reset_rate_limit()
        assert notifier._is_rate_limited() is False

    @pytest.mark.asyncio
    async def test_notify_returns_rate_limited_result(self, mock_jira_client, review_cycle_data):
        """Test that notify returns rate_limited=True when rate limited."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=60.0,
        )

        # First call should succeed
        result1 = await notifier.notify(review_cycle_data)
        assert result1.posted is True
        assert result1.rate_limited is False

        # Second immediate call should be rate limited
        result2 = await notifier.notify(review_cycle_data)
        assert result2.posted is False
        assert result2.rate_limited is True

        # Jira should only have been called once
        assert mock_jira_client.add_comment.call_count == 1


# ---------------------------------------------------------------------------
# Comment formatting tests
# ---------------------------------------------------------------------------


class TestCommentFormatting:
    """Tests for comment formatting."""

    def test_format_approved_verdict(self, notifier):
        """Test formatting for approved verdict."""
        data = ReviewCycleData(
            cycle=2,
            max_cycles=5,
            verdict="approved",
            feedback="All good!",
            skill="review-skill",
            elapsed_seconds=30.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        assert "Review Cycle 2/5" in comment
        assert "✅" in comment
        assert "APPROVED" in comment
        assert "review-skill" in comment
        assert "30.0s" in comment
        assert "All good!" in comment

    def test_format_rejected_verdict(self, notifier):
        """Test formatting for rejected verdict."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="Needs fixes",
            skill="code-review",
            elapsed_seconds=25.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        assert "Review Cycle 1/3" in comment
        assert "❌" in comment
        assert "REJECTED" in comment
        assert "Needs fixes" in comment

    def test_format_duration_minutes(self, notifier):
        """Test that duration >= 60s is formatted in minutes."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="test",
            elapsed_seconds=125.0,  # 2.08 minutes
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        assert "2.1m" in comment

    def test_format_duration_seconds(self, notifier):
        """Test that duration < 60s is formatted in seconds."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="test",
            elapsed_seconds=45.3,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        assert "45.3s" in comment

    def test_format_empty_feedback(self, notifier):
        """Test formatting with empty feedback."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="test",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        # Should not include "Feedback:" section
        assert "Feedback:" not in comment

    def test_format_whitespace_feedback(self, notifier):
        """Test formatting with whitespace-only feedback."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="   \n\t  ",
            skill="test",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        # Should not include "Feedback:" section
        assert "Feedback:" not in comment

    def test_format_empty_skill(self, notifier):
        """Test formatting with empty skill name."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Good",
            skill="",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        # Should not include "Skill:" section
        assert "Skill:" not in comment

    def test_format_truncates_long_feedback(self, notifier):
        """Test that long feedback is truncated."""
        long_feedback = "A" * (MAX_FEEDBACK_LENGTH + 100)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback=long_feedback,
            skill="test",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        # Should be truncated with ellipsis
        assert "..." in comment
        # Should not contain the full feedback
        assert long_feedback not in comment

    def test_format_truncates_at_word_boundary(self, notifier):
        """Test that truncation prefers word boundaries."""
        # Create feedback with words
        words = ["word"] * 150  # More than MAX_FEEDBACK_LENGTH
        long_feedback = " ".join(words)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback=long_feedback,
            skill="test",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        # Should end with "..." after a complete word
        assert "..." in comment
        # The truncated content should not cut a word in half
        feedback_part = comment.split("*Feedback:*")[1].strip()
        assert feedback_part.endswith("word...")

    def test_format_max_feedback_length_constant(self):
        """Test that MAX_FEEDBACK_LENGTH is 500."""
        assert MAX_FEEDBACK_LENGTH == 500

    def test_format_case_insensitive_verdict(self, notifier):
        """Test that verdict is uppercased in output."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="Approved",  # Mixed case
            feedback="",
            skill="test",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        comment = notifier._format_comment(data)

        assert "APPROVED" in comment


# ---------------------------------------------------------------------------
# Notify tests
# ---------------------------------------------------------------------------


class TestNotify:
    """Tests for notify method."""

    @pytest.mark.asyncio
    async def test_notify_posts_comment(self, mock_jira_client, notifier, review_cycle_data):
        """Test that notify posts a comment to Jira."""
        result = await notifier.notify(review_cycle_data)

        assert result.posted is True
        assert result.rate_limited is False
        assert result.error is None
        mock_jira_client.add_comment.assert_called_once()

        # Check the call args
        call_args = mock_jira_client.add_comment.call_args
        assert call_args[0][0] == "TEST-123"  # ticket_key
        assert "Review Cycle 1/3" in call_args[0][1]  # comment body

    @pytest.mark.asyncio
    async def test_notify_updates_last_notify_time(self, notifier, review_cycle_data):
        """Test that notify updates the last notify time."""
        assert notifier._last_notify_time is None

        await notifier.notify(review_cycle_data)

        assert notifier._last_notify_time is not None

    @pytest.mark.asyncio
    async def test_notify_handles_api_error(
        self, mock_jira_client, notifier, review_cycle_data, caplog
    ):
        """Test that notify handles Jira API errors gracefully."""
        mock_jira_client.add_comment.side_effect = Exception("API Error")

        with caplog.at_level(logging.WARNING):
            result = await notifier.notify(review_cycle_data)

        assert result.posted is False
        assert result.rate_limited is False
        assert result.error == "API Error"

        # Check warning was logged
        assert "Failed to post review comment" in caplog.text
        assert "TEST-123" in caplog.text

    @pytest.mark.asyncio
    async def test_notify_logs_success(self, notifier, review_cycle_data, caplog):
        """Test that notify logs success at INFO level."""
        with caplog.at_level(logging.INFO):
            await notifier.notify(review_cycle_data)

        assert "Posted review cycle 1/3 comment to TEST-123" in caplog.text

    @pytest.mark.asyncio
    async def test_notify_does_not_update_time_on_error(
        self, mock_jira_client, notifier, review_cycle_data
    ):
        """Test that last_notify_time is not updated on error."""
        mock_jira_client.add_comment.side_effect = Exception("API Error")

        await notifier.notify(review_cycle_data)

        # Time should not be updated on failure
        assert notifier._last_notify_time is None

    @pytest.mark.asyncio
    async def test_notify_rate_limited_logs_debug(
        self, mock_jira_client, review_cycle_data, caplog
    ):
        """Test that rate limited skips log at DEBUG level."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=60.0,
        )

        # First call
        await notifier.notify(review_cycle_data)

        # Second call should be rate limited
        with caplog.at_level(logging.DEBUG):
            result = await notifier.notify(review_cycle_data)

        assert result.rate_limited is True
        assert "Rate limited" in caplog.text


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration-style tests combining multiple features."""

    @pytest.mark.asyncio
    async def test_multiple_cycles_with_rate_limiting(self, mock_jira_client):
        """Test posting multiple cycles respects rate limiting."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=0.1,  # Short for testing
        )

        cycle1 = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="First review",
            skill="review",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        cycle2 = ReviewCycleData(
            cycle=2,
            max_cycles=3,
            verdict="approved",
            feedback="Second review",
            skill="review",
            elapsed_seconds=15.0,
            timestamp="2024-01-15T10:31:00Z",
        )

        # First cycle posts
        result1 = await notifier.notify(cycle1)
        assert result1.posted is True

        # Immediate second cycle is rate limited
        result2 = await notifier.notify(cycle2)
        assert result2.rate_limited is True

        # Wait for rate limit to expire
        import asyncio

        await asyncio.sleep(0.15)

        # Third attempt should succeed
        result3 = await notifier.notify(cycle2)
        assert result3.posted is True

        # Verify two comments were posted
        assert mock_jira_client.add_comment.call_count == 2

    @pytest.mark.asyncio
    async def test_error_does_not_block_subsequent_posts(self, mock_jira_client):
        """Test that an error on one call doesn't block future calls."""
        notifier = ReviewJiraNotifier(
            jira_client=mock_jira_client,
            ticket_key="TEST-123",
            rate_limit_seconds=0.0,  # No rate limiting
        )

        cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Test",
            skill="review",
            elapsed_seconds=10.0,
            timestamp="2024-01-15T10:30:00Z",
        )

        # First call fails
        mock_jira_client.add_comment.side_effect = Exception("API Error")
        result1 = await notifier.notify(cycle)
        assert result1.posted is False
        assert result1.error == "API Error"

        # Second call succeeds
        mock_jira_client.add_comment.side_effect = None
        result2 = await notifier.notify(cycle)
        assert result2.posted is True
        assert result2.error is None

    @pytest.mark.asyncio
    async def test_comment_contains_all_cycle_info(self, mock_jira_client, notifier):
        """Test that posted comment contains all relevant info."""
        data = ReviewCycleData(
            cycle=2,
            max_cycles=5,
            verdict="rejected",
            feedback="Please fix the typo on line 42",
            skill="code-review",
            elapsed_seconds=90.5,
            timestamp="2024-01-15T10:30:00Z",
        )

        await notifier.notify(data)

        call_args = mock_jira_client.add_comment.call_args
        comment_body = call_args[0][1]

        # Verify all key information is present
        assert "2/5" in comment_body  # cycle/max_cycles
        assert "REJECTED" in comment_body  # verdict
        assert "❌" in comment_body  # verdict icon
        assert "code-review" in comment_body  # skill
        assert "1.5m" in comment_body  # duration (90.5s = 1.5m)
        assert "typo on line 42" in comment_body  # feedback
