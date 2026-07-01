"""Jira notifier for review cycle events.

This module provides the ReviewJiraNotifier class for posting Jira comments
when review cycles are detected during container execution.

The notifier includes rate limiting to prevent comment spam and handles
Jira API errors gracefully by logging and continuing without failing.

Usage:
    notifier = ReviewJiraNotifier(
        jira_client=jira_client,
        ticket_key="PROJ-123",
    )

    # Post a comment for a review cycle
    await notifier.notify(cycle_data)

    # With custom rate limit interval
    notifier = ReviewJiraNotifier(
        jira_client=jira_client,
        ticket_key="PROJ-123",
        rate_limit_seconds=60.0,
    )
"""

import logging
import time
from dataclasses import dataclass

from forge.integrations.jira import JiraClient
from forge.observability.review_poller import ReviewCycleData

logger = logging.getLogger(__name__)

# Default rate limit interval between comments (seconds)
DEFAULT_RATE_LIMIT_SECONDS = 30.0

# Maximum length for feedback in comments before truncation
MAX_FEEDBACK_LENGTH = 500


@dataclass
class NotifyResult:
    """Result of a notify attempt.

    Attributes:
        posted: Whether the comment was successfully posted.
        rate_limited: Whether the attempt was skipped due to rate limiting.
        error: Error message if posting failed, None otherwise.
    """

    posted: bool
    rate_limited: bool
    error: str | None = None


class ReviewJiraNotifier:
    """Posts Jira comments for review cycle events with rate limiting.

    This class handles posting review cycle information as Jira comments,
    including cycle number, verdict, feedback summary, skill name, and duration.

    Rate limiting prevents comment spam by enforcing a configurable interval
    between comments. Jira API errors are logged but don't cause failures.

    Attributes:
        jira_client: The JiraClient instance for API calls.
        ticket_key: The Jira ticket key to post comments to.
        rate_limit_seconds: Minimum seconds between comments.
    """

    def __init__(
        self,
        jira_client: JiraClient,
        ticket_key: str,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    ):
        """Initialize the review notifier.

        Args:
            jira_client: JiraClient instance for posting comments.
            ticket_key: The Jira ticket key (e.g., "PROJ-123") for comments.
            rate_limit_seconds: Minimum seconds between comments. Default 30s.
        """
        self.jira_client = jira_client
        self.ticket_key = ticket_key
        self.rate_limit_seconds = rate_limit_seconds
        self._last_notify_time: float | None = None

    def _is_rate_limited(self) -> bool:
        """Check if we should skip due to rate limiting.

        Returns:
            True if rate limited (should skip), False if can proceed.
        """
        if self._last_notify_time is None:
            return False

        elapsed = time.monotonic() - self._last_notify_time
        return elapsed < self.rate_limit_seconds

    def _format_comment(self, cycle_data: ReviewCycleData) -> str:
        """Format review cycle data as a readable Jira comment.

        Args:
            cycle_data: The review cycle data to format.

        Returns:
            Formatted comment string.
        """
        # Build the verdict display
        verdict_upper = cycle_data.verdict.upper()
        verdict_icon = "✅" if verdict_upper == "APPROVED" else "❌"

        # Format duration
        elapsed = cycle_data.elapsed_seconds
        duration_str = f"{elapsed / 60:.1f}m" if elapsed >= 60 else f"{elapsed:.1f}s"

        # Build header
        lines = [
            f"*Review Cycle {cycle_data.cycle}/{cycle_data.max_cycles}* — {verdict_icon} *{verdict_upper}*",
            "",
        ]

        # Add skill and duration
        if cycle_data.skill:
            lines.append(f"*Skill:* {cycle_data.skill}")
        lines.append(f"*Duration:* {duration_str}")

        # Add feedback summary (truncated if too long)
        feedback = cycle_data.feedback.strip()
        if feedback:
            lines.append("")
            lines.append("*Feedback:*")

            # Truncate if too long
            if len(feedback) > MAX_FEEDBACK_LENGTH:
                truncated = feedback[:MAX_FEEDBACK_LENGTH].rsplit(" ", 1)[0]
                if len(truncated) < MAX_FEEDBACK_LENGTH // 2:
                    # No good word boundary, just cut
                    truncated = feedback[:MAX_FEEDBACK_LENGTH]
                lines.append(f"{truncated}...")
            else:
                lines.append(feedback)

        return "\n".join(lines)

    async def notify(self, cycle_data: ReviewCycleData) -> NotifyResult:
        """Post a Jira comment for a review cycle.

        Handles rate limiting and API errors gracefully. If rate limited,
        returns without posting. If API call fails, logs the error and
        returns without raising.

        Args:
            cycle_data: The review cycle data to post.

        Returns:
            NotifyResult indicating success, rate limiting, or error.
        """
        # Check rate limiting
        if self._is_rate_limited():
            remaining = self.rate_limit_seconds - (time.monotonic() - (self._last_notify_time or 0))
            logger.debug(
                f"Rate limited: skipping Jira comment for {self.ticket_key} (wait {remaining:.1f}s)"
            )
            return NotifyResult(posted=False, rate_limited=True)

        # Format the comment
        comment_body = self._format_comment(cycle_data)

        # Post to Jira with error handling
        try:
            await self.jira_client.add_comment(self.ticket_key, comment_body)
            self._last_notify_time = time.monotonic()
            logger.info(
                f"Posted review cycle {cycle_data.cycle}/{cycle_data.max_cycles} "
                f"comment to {self.ticket_key}"
            )
            return NotifyResult(posted=True, rate_limited=False)
        except Exception as e:
            # Log and continue - don't fail the container
            logger.warning(f"Failed to post review comment to {self.ticket_key}: {e}")
            return NotifyResult(posted=False, rate_limited=False, error=str(e))

    def reset_rate_limit(self) -> None:
        """Reset the rate limit timer.

        Call this to allow immediate posting after a long pause.
        """
        self._last_notify_time = None
