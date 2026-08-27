"""Webhook event models for Jira and GitHub integrations."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventSource(StrEnum):
    """Source of webhook events."""

    JIRA = "jira"
    SOURCE_CONTROL = "source_control"


class EventStatus(StrEnum):
    """Processing status for webhook events."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass
class WebhookEvent:
    """Represents an incoming webhook event from Jira or GitHub."""

    event_id: str
    source: EventSource
    event_type: str
    ticket_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=datetime.utcnow)
    processed_at: datetime | None = None
    status: EventStatus = EventStatus.PENDING
    error_message: str | None = None

    def mark_processing(self) -> None:
        """Mark event as currently being processed."""
        self.status = EventStatus.PROCESSING

    def mark_completed(self) -> None:
        """Mark event as successfully processed."""
        self.status = EventStatus.COMPLETED
        self.processed_at = datetime.utcnow()

    def mark_failed(self, error: str) -> None:
        """Mark event as failed with error message."""
        self.status = EventStatus.FAILED
        self.error_message = error
        self.processed_at = datetime.utcnow()

    def mark_duplicate(self) -> None:
        """Mark event as duplicate (already processed)."""
        self.status = EventStatus.DUPLICATE
        self.processed_at = datetime.utcnow()
