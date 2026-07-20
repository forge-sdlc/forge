"""Webhook deduplication middleware using Redis."""

import hashlib
import logging

import redis.asyncio as redis

from forge.orchestrator.checkpointer import get_redis_client
from forge.queue.deduplication import DEDUP_KEY_PREFIX, DEDUP_TTL_SECONDS

logger = logging.getLogger(__name__)


class DeduplicationService:
    """Service for checking and recording event deduplication."""

    def __init__(self, redis_client: redis.Redis | None = None):
        """Initialize deduplication service.

        Args:
            redis_client: Optional Redis client. Creates new if not provided.
        """
        self._redis = redis_client

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def is_duplicate(self, event_id: str) -> bool:
        """Check if an event has already been processed.

        Args:
            event_id: Unique event identifier.

        Returns:
            True if event has been seen before.
        """
        redis_client = await self._get_redis()
        key = f"{DEDUP_KEY_PREFIX}{event_id}"

        exists = await redis_client.exists(key)
        return bool(exists)

    async def mark_processed(self, event_id: str) -> None:
        """Mark an event as processed.

        Args:
            event_id: Unique event identifier.
        """
        redis_client = await self._get_redis()
        key = f"{DEDUP_KEY_PREFIX}{event_id}"

        await redis_client.setex(key, DEDUP_TTL_SECONDS, "1")
        logger.debug(f"Marked event {event_id} as processed")

    async def check_and_mark(self, event_id: str) -> bool:
        """Atomically check and mark an event.

        Uses Redis SETNX for atomic check-and-set.

        Args:
            event_id: Unique event identifier.

        Returns:
            True if this is a NEW event (not duplicate).
            False if this is a duplicate.
        """
        redis_client = await self._get_redis()
        key = f"{DEDUP_KEY_PREFIX}{event_id}"

        # SETNX returns True if key was set (new event)
        is_new = await redis_client.setnx(key, "1")

        if is_new:
            # Set expiration on new keys
            await redis_client.expire(key, DEDUP_TTL_SECONDS)
            logger.debug(f"New event {event_id} marked for processing")
        else:
            logger.debug(f"Duplicate event {event_id} detected")

        return bool(is_new)


def generate_idempotency_key(
    source: str,
    ticket_key: str,
    event_type: str,
    payload_hash: str,
) -> str:
    """Generate an idempotency key for deduplication.

    This is useful when the original event_id is not available
    or for additional deduplication based on content.

    Args:
        source: Event source (jira, github).
        ticket_key: Associated ticket key.
        event_type: Type of event.
        payload_hash: Hash of relevant payload fields.

    Returns:
        Deterministic idempotency key.
    """
    content = f"{source}:{ticket_key}:{event_type}:{payload_hash}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]


# Singleton instance
_dedup_service: DeduplicationService | None = None


async def get_dedup_service() -> DeduplicationService:
    """Get the singleton deduplication service."""
    global _dedup_service
    if _dedup_service is None:
        _dedup_service = DeduplicationService()
    return _dedup_service
