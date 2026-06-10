"""Webhook retry queue with dead-letter handling."""

import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from forge.orchestrator.checkpointer import get_redis_client
from forge.queue.models import QueueMessage

logger = logging.getLogger(__name__)

# Redis key prefixes
RETRY_QUEUE_KEY = "forge:retry:queue"
DEAD_LETTER_KEY = "forge:retry:dlq"
RETRY_ATTEMPTS_KEY = "forge:retry:attempts"

# Retry configuration
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_MULTIPLIER = 2
INITIAL_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 3600  # 1 hour


@dataclass
class RetryEntry:
    """An entry in the retry queue."""

    message: QueueMessage
    attempt: int
    next_retry: datetime
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        msg = self.message.to_dict()
        # Preserve message_id separately so round-trip is lossless.
        msg["message_id"] = self.message.message_id
        return {
            "message": msg,
            "attempt": self.attempt,
            "next_retry": self.next_retry.isoformat(),
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetryEntry":
        """Create from dictionary loaded from Redis."""
        msg_data = dict(data["message"])  # shallow copy — do not mutate the caller's dict
        message_id = msg_data.pop("message_id", "")
        return cls(
            message=QueueMessage.from_redis(message_id, msg_data),
            attempt=data["attempt"],
            next_retry=datetime.fromisoformat(data["next_retry"]),
            last_error=data["last_error"],
        )


class RetryQueue:
    """Manages webhook retry queue with exponential backoff and dead-letter."""

    def __init__(self):
        """Initialize retry queue."""
        self._redis = None

    async def _get_redis(self):
        """Get Redis client."""
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def enqueue_for_retry(
        self,
        message: QueueMessage,
        error: str,
    ) -> bool:
        """Add a failed message to the retry queue.

        Args:
            message: The failed message.
            error: Error message from the failure.

        Returns:
            True if queued for retry, False if moved to dead-letter.
        """
        redis = await self._get_redis()
        message_id = f"{message.source}:{message.ticket_key}:{message.event_id}"

        # Get current attempt count
        attempt_key = f"{RETRY_ATTEMPTS_KEY}:{message_id}"
        attempt = await redis.incr(attempt_key)
        await redis.expire(attempt_key, 86400)  # 24 hour TTL

        if attempt > MAX_RETRY_ATTEMPTS:
            # Move to dead-letter queue
            await self._move_to_dead_letter(message, error, attempt)
            return False

        # Calculate next retry time with exponential backoff
        delay = min(
            INITIAL_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_MULTIPLIER ** (attempt - 1)),
            MAX_RETRY_DELAY_SECONDS,
        )
        next_retry = datetime.utcnow() + timedelta(seconds=delay)

        entry = RetryEntry(
            message=message,
            attempt=attempt,
            next_retry=next_retry,
            last_error=error,
        )

        # Add to retry queue with score = next retry timestamp
        await redis.zadd(
            RETRY_QUEUE_KEY,
            {json.dumps(entry.to_dict()): next_retry.timestamp()},
        )

        logger.info(f"Queued {message_id} for retry {attempt}/{MAX_RETRY_ATTEMPTS} in {delay:.0f}s")
        return True

    async def _move_to_dead_letter(
        self,
        message: QueueMessage,
        error: str,
        attempt: int,
    ) -> None:
        """Move a message to the dead-letter queue.

        Args:
            message: The failed message.
            error: Final error message.
            attempt: Number of attempts made.
        """
        redis = await self._get_redis()
        message_id = f"{message.source}:{message.ticket_key}:{message.event_id}"

        msg = message.to_dict()
        # Preserve message_id so round-trip through requeue_dead_letter is lossless.
        msg["message_id"] = message.message_id
        entry = {
            "message": msg,
            "error": error,
            "attempts": attempt,
            "failed_at": datetime.utcnow().isoformat(),
        }

        await redis.rpush(DEAD_LETTER_KEY, json.dumps(entry))
        logger.warning(
            f"Message {message_id} moved to dead-letter queue after "
            f"{attempt} attempts. Error: {error}"
        )

    async def get_due_messages(self, limit: int = 10) -> list[RetryEntry]:
        """Get messages that are due for retry.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of retry entries ready to be processed.
        """
        redis = await self._get_redis()
        now = datetime.utcnow().timestamp()

        # Get messages with score <= now
        entries = await redis.zrangebyscore(
            RETRY_QUEUE_KEY,
            "-inf",
            now,
            start=0,
            num=limit,
        )

        results = []
        for entry_json in entries:
            try:
                data = json.loads(entry_json)
                results.append(RetryEntry.from_dict(data))
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Failed to parse retry entry: {e}")

        return results

    async def remove_from_retry(self, entry: RetryEntry) -> None:
        """Remove a message from the retry queue after successful processing.

        Removes the sorted-set entry **and** clears the attempt counter so the
        message starts fresh if it ever fails again.

        Args:
            entry: The retry entry to remove.
        """
        redis = await self._get_redis()
        await redis.zrem(RETRY_QUEUE_KEY, json.dumps(entry.to_dict()))

        # Clear attempt counter
        message_id = f"{entry.message.source}:{entry.message.ticket_key}:{entry.message.event_id}"
        await redis.delete(f"{RETRY_ATTEMPTS_KEY}:{message_id}")

    async def remove_from_retry_without_counter_reset(self, entry: RetryEntry) -> None:
        """Remove a message's sorted-set entry without clearing the attempt counter.

        Use this in the failure path so that the attempt counter keeps
        accumulating across re-enqueues.  If the counter were deleted here,
        ``enqueue_for_retry`` would call ``INCR`` on a missing key and Redis
        would reinitialise it to 1, preventing the message from ever reaching
        the dead-letter queue.

        Args:
            entry: The retry entry to remove from the sorted set.
        """
        redis = await self._get_redis()
        await redis.zrem(RETRY_QUEUE_KEY, json.dumps(entry.to_dict()))

    async def get_dead_letter_entries(
        self,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get entries from the dead-letter queue for inspection.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of dead-letter entries.
        """
        redis = await self._get_redis()
        entries = await redis.lrange(DEAD_LETTER_KEY, 0, limit - 1)

        results = []
        for entry_json in entries:
            with contextlib.suppress(json.JSONDecodeError):
                results.append(json.loads(entry_json))

        return results

    async def requeue_dead_letter(self, index: int) -> bool:
        """Requeue a dead-letter entry for retry.

        Args:
            index: Index of the entry in the dead-letter queue.

        Returns:
            True if successfully requeued.
        """
        redis = await self._get_redis()
        entries = await redis.lrange(DEAD_LETTER_KEY, index, index)

        if not entries:
            return False

        try:
            data = json.loads(entries[0])
            msg_data = dict(data["message"])  # shallow copy — do not mutate the caller's dict
            stream_entry_id = msg_data.pop("message_id", "")
            message = QueueMessage.from_redis(stream_entry_id, msg_data)

            # Reset attempt counter
            message_id = f"{message.source}:{message.ticket_key}:{message.event_id}"
            await redis.delete(f"{RETRY_ATTEMPTS_KEY}:{message_id}")

            # Add back to retry queue
            entry = RetryEntry(
                message=message,
                attempt=1,
                next_retry=datetime.utcnow() + timedelta(seconds=INITIAL_RETRY_DELAY_SECONDS),
                last_error="Requeued from dead-letter",
            )
            await redis.zadd(
                RETRY_QUEUE_KEY,
                {json.dumps(entry.to_dict()): entry.next_retry.timestamp()},
            )

            # Remove from dead-letter (by setting to tombstone and cleaning)
            await redis.lset(DEAD_LETTER_KEY, index, "__REMOVED__")
            await redis.lrem(DEAD_LETTER_KEY, 1, "__REMOVED__")

            logger.info(f"Requeued {message_id} from dead-letter queue")
            return True

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to requeue dead-letter entry: {e}")
            return False

    async def get_queue_stats(self) -> dict[str, int]:
        """Get statistics about the retry and dead-letter queues.

        Returns:
            Dictionary with queue statistics.
        """
        redis = await self._get_redis()
        return {
            "retry_queue_depth": await redis.zcard(RETRY_QUEUE_KEY),
            "dead_letter_depth": await redis.llen(DEAD_LETTER_KEY),
        }
