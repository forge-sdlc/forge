"""Queue message models for webhook event processing."""

import json
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime
from typing import Any

from forge.models.events import EventSource


@dataclass
class QueueMessage:
    """Represents a message in the Redis Streams queue."""

    message_id: str
    event_id: str
    source: EventSource
    event_type: str
    ticket_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    retry_count: int = 0

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for Redis storage.

        Returns:
            Dictionary with string values for Redis.
        """
        return {
            "event_id": self.event_id,
            "source": self.source.value,
            "event_type": self.event_type,
            "ticket_key": self.ticket_key,
            "payload": json.dumps(self.payload),
            "timestamp": self.timestamp.isoformat(),
            "retry_count": str(self.retry_count),
        }

    @classmethod
    def from_redis(cls, message_id: str, data: dict[str, str]) -> "QueueMessage":
        """Create from Redis stream entry.

        Args:
            message_id: Redis stream message ID.
            data: Message data from Redis.

        Returns:
            Populated QueueMessage instance.
        """
        return cls(
            message_id=message_id,
            event_id=data.get("event_id", ""),
            source=EventSource(data.get("source", "jira")),
            event_type=data.get("event_type", ""),
            ticket_key=data.get("ticket_key", ""),
            payload=json.loads(data.get("payload", "{}")),
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.utcnow().isoformat())),
            retry_count=int(data.get("retry_count", "0")),
        )

    def increment_retry(self) -> "QueueMessage":
        """Return a new message with incremented retry count."""
        return dataclass_replace(self, retry_count=self.retry_count + 1)
