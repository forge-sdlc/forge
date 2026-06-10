"""Queue consumer for processing webhook events from Redis Streams."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

import redis.asyncio as redis

from forge.config import get_settings
from forge.integrations.jira import JiraClient
from forge.models.events import EventSource
from forge.orchestrator.checkpointer import get_redis_client
from forge.queue.models import QueueMessage
from forge.queue.producer import GITHUB_STREAM, JIRA_STREAM
from forge.queue.retry import RetryQueue

logger = logging.getLogger(__name__)

# Consumer group name
CONSUMER_GROUP = "forge-workers"

# How often (seconds) the retry-queue poller wakes up
POLL_INTERVAL_SECONDS = 10

# Handler type for message processing
MessageHandler = Callable[[QueueMessage], Coroutine[Any, Any, None]]


class QueueConsumer:
    """Consumes webhook events from Redis Streams with FIFO ordering per ticket.

    Implements consumer groups for distributed processing and ensures
    events for the same ticket are processed sequentially.
    """

    def __init__(
        self,
        consumer_name: str,
        redis_client: redis.Redis | None = None,
        jira_client: JiraClient | None = None,
        max_concurrent_tasks: int | None = None,
    ):
        """Initialize the queue consumer.

        Args:
            consumer_name: Unique name for this consumer instance.
            redis_client: Optional Redis client. Creates new if not provided.
            jira_client: Optional Jira client for freshness checks.
            max_concurrent_tasks: Maximum concurrent in-flight tasks. Defaults
                to ``settings.queue_max_concurrent_tasks`` when not provided.
        """
        self.consumer_name = consumer_name
        self._redis = redis_client
        self._jira = jira_client
        self._handlers: dict[EventSource, MessageHandler] = {}
        self._running = False
        self._ticket_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        concurrency = (
            max_concurrent_tasks
            if max_concurrent_tasks is not None
            else get_settings().queue_max_concurrent_tasks
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._retry_queue = RetryQueue()

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def _ensure_consumer_groups(self) -> None:
        """Ensure consumer groups exist for all streams."""
        redis_client = await self._get_redis()

        for stream in [JIRA_STREAM, GITHUB_STREAM]:
            try:
                await redis_client.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
                logger.info(f"Created consumer group {CONSUMER_GROUP} for {stream}")
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
                # Group already exists

    def register_handler(self, source: EventSource, handler: MessageHandler) -> None:
        """Register a handler for events from a specific source.

        Args:
            source: Event source to handle.
            handler: Async function to process messages.
        """
        self._handlers[source] = handler
        logger.info(f"Registered handler for {source.value} events")

    async def _check_freshness(self, message: QueueMessage) -> bool:
        """Check if the event is still fresh (ticket state hasn't changed).

        Args:
            message: The message to check.

        Returns:
            True if the event should be processed, False if stale.
        """
        if self._jira is None or message.source != EventSource.JIRA:
            return True

        try:
            issue = await self._jira.get_issue(message.ticket_key)
            event_status = (
                message.payload.get("issue", {}).get("fields", {}).get("status", {}).get("name", "")
            )

            if issue.status != event_status:
                logger.info(
                    f"Stale event for {message.ticket_key}: "
                    f"event status {event_status}, current status {issue.status}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"Freshness check failed for {message.ticket_key}: {e}")
            return True  # Process anyway if check fails

    async def _ack(self, stream: str, message_id: str) -> None:
        """Acknowledge a message in the Redis stream.

        Args:
            stream: Redis stream name.
            message_id: Message ID to acknowledge.
        """
        redis_client = await self._get_redis()
        await redis_client.xack(stream, CONSUMER_GROUP, message_id)

    async def _process_message(
        self,
        message: QueueMessage,
        stream: str,
        *,
        raise_on_error: bool = False,
        skip_ack: bool = False,
    ) -> None:
        """Process a single message with FIFO ordering per ticket.

        Acquires the concurrency semaphore before running the handler and
        acknowledges the message in Redis only on success. Errors are logged;
        when ``raise_on_error`` is True the exception is also re-raised so the
        caller can react (used by the retry path).  When ``skip_ack`` is True
        the internal xack call is skipped; the caller is then responsible for
        acknowledging the message (used by the retry path which issues its own
        xack after removing the retry-queue entry).

        Args:
            message: The message to process.
            stream: Redis stream name (needed for xack).
            raise_on_error: If True, re-raise exceptions after logging them.
                Used by the retry path so callers can detect handler failures.
            skip_ack: If True, do not call xack on success.  The caller
                handles acknowledgement.
        """
        handler = self._handlers.get(message.source)
        if handler is None:
            logger.warning(f"No handler for {message.source.value} events")
            # Ack so the un-handleable message does not fill the PEL.
            if not skip_ack:
                await self._ack(stream, message.message_id)
            return

        # Semaphore caps peak concurrency; per-ticket lock ensures FIFO ordering.
        async with self._semaphore, self._ticket_locks[message.ticket_key]:
            # Check freshness before processing
            if not await self._check_freshness(message):
                logger.info(f"Skipping stale event {message.event_id}")
                # Ack stale messages so they don't linger in the PEL.
                if not skip_ack:
                    await self._ack(stream, message.message_id)
                return

            try:
                await handler(message)
                logger.info(f"Processed event {message.event_id}")
                if not skip_ack:
                    await self._ack(stream, message.message_id)
            except Exception as e:
                logger.error(f"Error processing {message.event_id}: {e}")
                if raise_on_error:
                    raise
                # Stream consumer path: enqueue for retry so the message is
                # not lost.  If enqueue_for_retry returns False the message was
                # moved to the dead-letter queue; xack it to clear the PEL.
                try:
                    moved_to_dlq = not await self._retry_queue.enqueue_for_retry(message, str(e))
                    if moved_to_dlq:
                        await self._ack(stream, message.message_id)
                except Exception as retry_err:
                    logger.error(
                        f"Failed to enqueue {message.event_id} for retry: {retry_err}. "
                        "Message remains in PEL for reclaim."
                    )

    async def _consume_stream(self, stream: str, _source: EventSource) -> None:
        """Consume messages from a single stream.

        Args:
            stream: Redis stream name.
            _source: Event source for the stream (unused, for API compatibility).
        """
        redis_client = await self._get_redis()

        while self._running:
            try:
                # Read from consumer group
                messages = await redis_client.xreadgroup(
                    CONSUMER_GROUP,
                    self.consumer_name,
                    {stream: ">"},
                    count=10,
                    block=5000,  # 5 second timeout
                )

                for _stream_name, entries in messages:
                    for message_id, data in entries:
                        message = QueueMessage.from_redis(message_id, data)
                        task = asyncio.create_task(
                            self._process_message(message, stream),
                            name=f"process-{message.event_id}",
                        )
                        self._active_tasks.add(task)
                        task.add_done_callback(self._active_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error consuming from {stream}: {e}")
                await asyncio.sleep(1)  # Brief pause before retry

        # Drain any in-flight processing tasks before returning
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def _process_retry_queue(self) -> None:
        """Poll the retry queue and re-dispatch due messages.

        Runs as a background task alongside the stream consumers.  Polls on a
        fixed interval so retries are dispatched once their backoff window has
        elapsed.
        """
        while self._running:
            try:
                entries = await self._retry_queue.get_due_messages()
                for entry in entries:
                    retry_stream = (
                        JIRA_STREAM if entry.message.source == EventSource.JIRA else GITHUB_STREAM
                    )
                    try:
                        await self._process_message(
                            entry.message, retry_stream, raise_on_error=True, skip_ack=True
                        )
                    except Exception as e:
                        logger.warning(
                            f"Retry attempt {entry.attempt} failed for "
                            f"{entry.message.ticket_key}:{entry.message.event_id}: {e}"
                        )
                        # Remove only the sorted-set entry — do NOT delete the attempt
                        # counter key.  enqueue_for_retry will INCR the existing key so
                        # the counter keeps accumulating and the message can eventually
                        # reach the dead-letter queue.
                        await self._retry_queue.remove_from_retry_without_counter_reset(entry)
                        await self._retry_queue.enqueue_for_retry(entry.message, str(e))
                        continue

                    # Message processing succeeded — clean up retry state and
                    # acknowledge the original stream entry (best-effort).
                    await self._retry_queue.remove_from_retry(entry)
                    logger.info(
                        f"Retry succeeded for {entry.message.ticket_key}:"
                        f"{entry.message.event_id} (attempt {entry.attempt})"
                    )
                    stream = (
                        JIRA_STREAM if entry.message.source == EventSource.JIRA else GITHUB_STREAM
                    )
                    try:
                        redis_client = await self._get_redis()
                        await redis_client.xack(stream, CONSUMER_GROUP, entry.message.message_id)
                    except Exception as xack_err:
                        # xack is best-effort: the message was already successfully
                        # processed and removed from the retry queue.  A failure here
                        # only means the PEL entry lingers; it will not be reprocessed
                        # because the retry-queue entry is gone.
                        logger.warning(
                            f"xack failed for {entry.message.event_id} after successful "
                            f"retry (PEL entry may linger): {xack_err}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in retry queue poller: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def start(self) -> None:
        """Start consuming from all registered streams."""
        await self._ensure_consumer_groups()
        self._running = True

        tasks = []
        if EventSource.JIRA in self._handlers:
            tasks.append(self._consume_stream(JIRA_STREAM, EventSource.JIRA))
        if EventSource.GITHUB in self._handlers:
            tasks.append(self._consume_stream(GITHUB_STREAM, EventSource.GITHUB))

        if tasks:
            tasks.append(self._process_retry_queue())
            logger.info(f"Consumer {self.consumer_name} starting...")
            await asyncio.gather(*tasks)

    async def stop(self) -> None:
        """Stop consuming messages and drain all in-flight tasks.

        Sets _running to False so the consume loop exits on the next poll
        timeout, then waits for every dispatched task to finish before
        returning. This ensures messages are not abandoned un-acked in the
        Redis PEL on shutdown.
        """
        self._running = False
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        logger.info(f"Consumer {self.consumer_name} stopped")
