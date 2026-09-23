"""Unit tests for event models."""

from datetime import UTC, datetime

import pytest

from forge.models.events import (
    EventSource,
    EventStatus,
    WebhookEvent,
)


class TestEventSource:
    """Tests for EventSource enum."""

    def test_event_sources_exist(self):
        """Verify event sources are defined."""
        assert EventSource.JIRA.value == "jira"
        assert EventSource.SOURCE_CONTROL.value == "source_control"


class TestEventStatus:
    """Tests for EventStatus enum."""

    def test_event_statuses_exist(self):
        """Verify event statuses are defined."""
        assert EventStatus.PENDING.value == "pending"
        assert EventStatus.PROCESSING.value == "processing"
        assert EventStatus.COMPLETED.value == "completed"
        assert EventStatus.FAILED.value == "failed"
        assert EventStatus.DUPLICATE.value == "duplicate"


class TestWebhookEvent:
    """Tests for WebhookEvent dataclass."""

    def test_create_jira_event(self):
        """Create a Jira webhook event."""
        event = WebhookEvent(
            event_id="evt-001",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            payload={"issue": {"key": "TEST-123"}},
        )
        assert event.event_id == "evt-001"
        assert event.source == EventSource.JIRA
        assert event.event_type == "jira:issue_updated"
        assert event.ticket_key == "TEST-123"
        assert event.status == EventStatus.PENDING

    def test_create_github_event(self):
        """Create a GitHub webhook event."""
        event = WebhookEvent(
            event_id="evt-002",
            source=EventSource.SOURCE_CONTROL,
            event_type="check_run",
            ticket_key="TEST-123",
            payload={"action": "completed"},
        )
        assert event.source == EventSource.SOURCE_CONTROL
        assert event.event_type == "check_run"

    def test_default_status_is_pending(self):
        """Default status is PENDING."""
        event = WebhookEvent(
            event_id="evt-003",
            source=EventSource.JIRA,
            event_type="jira:issue_created",
            ticket_key="TEST-456",
        )
        assert event.status == EventStatus.PENDING

    def test_mark_processing(self):
        """Event can be marked as processing."""
        event = WebhookEvent(
            event_id="evt-004",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-789",
        )

        event.mark_processing()

        assert event.status == EventStatus.PROCESSING

    def test_mark_completed(self):
        """Event can be marked as completed."""
        event = WebhookEvent(
            event_id="evt-005",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
        )

        event.mark_completed()

        assert event.status == EventStatus.COMPLETED
        assert event.processed_at is not None

    def test_mark_failed(self):
        """Event can be marked as failed with error."""
        event = WebhookEvent(
            event_id="evt-006",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
        )

        event.mark_failed("Connection timeout")

        assert event.status == EventStatus.FAILED
        assert event.error_message == "Connection timeout"
        assert event.processed_at is not None

    def test_mark_duplicate(self):
        """Event can be marked as duplicate."""
        event = WebhookEvent(
            event_id="evt-007",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
        )

        event.mark_duplicate()

        assert event.status == EventStatus.DUPLICATE
        assert event.processed_at is not None

    def test_event_has_received_at(self):
        """Event has received_at timestamp."""
        event = WebhookEvent(
            event_id="evt-008",
            source=EventSource.SOURCE_CONTROL,
            event_type="pull_request",
            ticket_key="TEST-456",
        )

        assert event.received_at is not None
        assert isinstance(event.received_at, datetime)

    def test_event_processed_at_initially_none(self):
        """Event processed_at is initially None."""
        event = WebhookEvent(
            event_id="evt-009",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
        )

        assert event.processed_at is None

    @pytest.mark.filterwarnings("error::DeprecationWarning")
    def test_received_at_uses_current_utc_time(self):
        before = datetime.now(UTC)
        event = WebhookEvent(
            event_id="evt-utc",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
        )
        after = datetime.now(UTC)

        assert event.received_at.tzinfo is UTC
        assert before <= event.received_at <= after

    @pytest.mark.filterwarnings("error::DeprecationWarning")
    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("mark_completed", ()),
            ("mark_failed", ("Connection timeout",)),
            ("mark_duplicate", ()),
        ],
    )
    def test_terminal_status_uses_current_utc_time(self, method, args):
        event = WebhookEvent(
            event_id="evt-utc",
            source=EventSource.JIRA,
            event_type="jira:issue_updated",
            ticket_key="TEST-123",
            received_at=datetime.now(UTC),
        )
        before = datetime.now(UTC)
        getattr(event, method)(*args)
        after = datetime.now(UTC)

        assert event.processed_at is not None
        assert event.processed_at.tzinfo is UTC
        assert event.received_at <= before <= event.processed_at <= after
