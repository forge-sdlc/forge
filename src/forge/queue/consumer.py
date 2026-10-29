"""Queue consumer for processing webhook events from Redis Streams."""

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis
from redis.exceptions import LockNotOwnedError

from forge.config import get_settings
from forge.integrations.jira import JiraClient
from forge.models.events import EventSource
from forge.orchestrator.checkpointer import get_redis_client
from forge.orchestrator.ownership import TicketOwnership, ownership_scope
from forge.queue.metrics import (
    QUEUE_ACTIVE_DELIVERIES,
    QUEUE_BUFFERED_DELIVERIES,
    QUEUE_LEASE_LOSSES,
    QUEUE_RECLAIMED_DELIVERIES,
)
from forge.queue.models import QueueMessage
from forge.queue.producer import JIRA_STREAM, SOURCE_CONTROL_STREAM
from forge.queue.retry import (
    RETRY_CLAIM_LEASE_SECONDS,
    RETRY_CLAIM_RENEW_SECONDS,
    RetryEntry,
    RetryQueue,
)

logger = logging.getLogger(__name__)

# Consumer group name
CONSUMER_GROUP = "forge-workers"

# How often (seconds) the retry-queue poller wakes up
POLL_INTERVAL_SECONDS = 10

# Ticket handlers can run for several minutes. The lease prevents a crashed
# worker from blocking a ticket forever, while the renewal task keeps live
# workers protected for arbitrarily long runs.
TICKET_LOCK_LEASE_SECONDS = 600
TICKET_LOCK_RENEW_SECONDS = 60
TICKET_LOCK_KEY_PREFIX = "forge:queue:ticket-lock:"
TICKET_LOCK_RENEW_TIMEOUT_SECONDS = 5
TICKET_LOCK_EXPIRY_SAFETY_SECONDS = 5
STREAM_DELIVERY_RENEW_SECONDS = 10

_RENEW_PENDING_DELIVERY_SCRIPT = """
local pending = redis.call("XPENDING", KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
if #pending == 0 or pending[1][2] ~= ARGV[2] then return 0 end
local claimed = redis.call("XCLAIM", KEYS[1], ARGV[1], ARGV[2], 0, ARGV[3], "IDLE", 0, "JUSTID")
if #claimed == 0 then return 0 end
return 1
"""

# Handler type for message processing
MessageHandler = Callable[[QueueMessage], Coroutine[Any, Any, None]]
TerminalFailureHandler = Callable[[QueueMessage, str], Coroutine[Any, Any, None]]
PrepareMessage = Callable[[QueueMessage], Awaitable[QueueMessage | None]]


@dataclass(frozen=True)
class _Delivery:
    message: QueueMessage
    stream: str
    raise_on_error: bool = False
    skip_ack: bool = False
    retry_entry: RetryEntry | None = None
    renew_stream_delivery: bool = False


class TicketLockLostError(RuntimeError):
    """Raised when a worker loses its distributed per-ticket lease."""


class RetryLeaseLostError(RuntimeError):
    """Raised when a worker loses ownership of a claimed retry entry."""


class StreamDeliveryLostError(RuntimeError):
    """Raised when a worker loses ownership of a pending stream delivery."""


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
        terminal_failure_handler: TerminalFailureHandler | None = None,
        prepare_message: PrepareMessage | None = None,
        max_outstanding_deliveries: int | None = None,
        pending_idle_ms: int | None = None,
        shutdown_timeout_seconds: float | None = None,
    ):
        """Initialize the queue consumer.

        Args:
            consumer_name: Unique name for this consumer instance.
            redis_client: Optional Redis client. Creates new if not provided.
            jira_client: Optional Jira client for freshness checks.
            max_concurrent_tasks: Maximum concurrent in-flight tasks. Defaults
                to ``settings.queue_max_concurrent_tasks`` when not provided.
            terminal_failure_handler: Optional callback invoked after a message
                is moved to the dead-letter queue.
            prepare_message: Optional canonicalization callback. It runs before
                ticket locks. Return ``None`` to durably ignore an unresolvable
                message; raise to schedule transient failures.
            max_outstanding_deliveries: Bound for active plus buffered deliveries.
            pending_idle_ms: Minimum PEL idle time before a delivery is recovered.
            shutdown_timeout_seconds: Maximum time to drain active deliveries.
        """
        self.consumer_name = consumer_name
        self._redis = redis_client
        self._jira = jira_client
        self._handlers: dict[EventSource, MessageHandler] = {}
        self._running = False
        self._ticket_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ticket_lock_users: dict[str, int] = defaultdict(int)
        concurrency = (
            max_concurrent_tasks
            if max_concurrent_tasks is not None
            else get_settings().queue_max_concurrent_tasks
        )
        if concurrency < 1:
            raise ValueError("max_concurrent_tasks must be positive")
        self._semaphore = asyncio.Semaphore(concurrency)
        settings = get_settings()
        self._max_outstanding_deliveries = (
            max_outstanding_deliveries
            if max_outstanding_deliveries is not None
            else settings.queue_max_outstanding_deliveries
        )
        if self._max_outstanding_deliveries < 2:
            raise ValueError("max_outstanding_deliveries must be at least 2")
        self._recovery_reserve = 1
        self._capacity_condition = asyncio.Condition()
        self._reserved_deliveries = 0
        self._pending_idle_ms = (
            pending_idle_ms if pending_idle_ms is not None else settings.queue_pending_idle_ms
        )
        self._shutdown_timeout_seconds = (
            shutdown_timeout_seconds
            if shutdown_timeout_seconds is not None
            else settings.queue_shutdown_timeout_seconds
        )
        if self._pending_idle_ms < 1 or self._shutdown_timeout_seconds <= 0:
            raise ValueError("pending idle time and shutdown timeout must be positive")
        self._prepare_message = prepare_message
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._pending_scan_cursors: dict[str, str] = {}
        self._retry_queue = RetryQueue(redis_client=redis_client)
        self._terminal_failure_handler = terminal_failure_handler

    @asynccontextmanager
    async def _distributed_ticket_lock(self, ticket_key: str) -> AsyncIterator[None]:
        """Hold a renewable, deployment-wide lease for one ticket."""
        redis_client = await self._get_redis()
        lock_key = f"{TICKET_LOCK_KEY_PREFIX}{ticket_key}"
        lock = redis_client.lock(
            lock_key,
            timeout=TICKET_LOCK_LEASE_SECONDS,
            blocking=True,
            blocking_timeout=None,
        )
        acquired = await lock.acquire()
        if not acquired:  # Defensive: blocking acquisition normally cannot return False.
            raise RuntimeError(f"Failed to acquire distributed lock for {ticket_key}")

        owner_task = asyncio.current_task()
        lease_lost = asyncio.Event()
        last_renewed = time.monotonic()
        lease_deadline = last_renewed + max(
            0.01,
            TICKET_LOCK_LEASE_SECONDS - TICKET_LOCK_EXPIRY_SAFETY_SECONDS,
        )

        def lose_lease(reason: str) -> None:
            logger.critical("Lost distributed ticket lock for %s: %s", ticket_key, reason)
            QUEUE_LEASE_LOSSES.labels(kind="ticket").inc()
            lease_lost.set()
            if owner_task is not None:
                owner_task.cancel()

        async def renew_lease() -> None:
            nonlocal last_renewed, lease_deadline
            while True:
                sleep_for = min(
                    TICKET_LOCK_RENEW_SECONDS,
                    max(0.001, lease_deadline - time.monotonic()),
                )
                await asyncio.sleep(sleep_for)
                if time.monotonic() >= lease_deadline:
                    lose_lease("renewal deadline elapsed")
                    return
                try:
                    extended = await asyncio.wait_for(
                        lock.extend(TICKET_LOCK_LEASE_SECONDS, replace_ttl=True),
                        timeout=min(
                            TICKET_LOCK_RENEW_TIMEOUT_SECONDS,
                            max(0.001, lease_deadline - time.monotonic()),
                        ),
                    )
                    if not extended:
                        lose_lease("Redis rejected lease renewal")
                        return
                    last_renewed = time.monotonic()
                    lease_deadline = last_renewed + max(
                        0.01,
                        TICKET_LOCK_LEASE_SECONDS - TICKET_LOCK_EXPIRY_SAFETY_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except LockNotOwnedError:
                    lose_lease("Redis reports LockNotOwnedError")
                    return
                except Exception:
                    logger.exception("Failed to renew distributed ticket lock for %s", ticket_key)
                    if time.monotonic() >= lease_deadline:
                        lose_lease("renewal errors exhausted the lease deadline")
                        return
                    await asyncio.sleep(min(1.0, max(0.001, lease_deadline - time.monotonic())))

        renewal_task = asyncio.create_task(renew_lease(), name=f"renew-ticket-lock-{ticket_key}")
        try:
            token = getattr(getattr(lock, "local", None), "token", None)
            if token is None:
                raise RuntimeError("Redis ticket lock did not expose its ownership token")
            with ownership_scope(TicketOwnership(redis_client, lock_key, token)):
                yield
        except asyncio.CancelledError:
            if lease_lost.is_set():
                raise TicketLockLostError(
                    f"Lost distributed ticket lock for {ticket_key}"
                ) from None
            raise
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            try:
                await lock.release()
            except LockNotOwnedError:
                logger.warning("Distributed ticket lock already expired for %s", ticket_key)
            except Exception:
                logger.exception("Failed to release distributed ticket lock for %s", ticket_key)

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def _ensure_consumer_groups(self) -> None:
        """Ensure consumer groups exist for all streams."""
        redis_client = await self._get_redis()

        for stream in [JIRA_STREAM, SOURCE_CONTROL_STREAM]:
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

    async def _reserve_capacity(
        self, requested: int, *, recovery: bool, allow_stopped: bool = False
    ) -> int:
        """Reserve bounded in-flight capacity before reading or claiming work."""
        if requested <= 0:
            return 0
        async with self._capacity_condition:
            while self._running or allow_stopped:
                limit = self._max_outstanding_deliveries - (
                    0 if recovery else self._recovery_reserve
                )
                available = limit - self._reserved_deliveries
                if available > 0:
                    count = min(requested, available)
                    self._reserved_deliveries += count
                    QUEUE_BUFFERED_DELIVERIES.inc(count)
                    return count
                await self._capacity_condition.wait()
        return 0

    async def _release_capacity(self, count: int = 1, *, buffered: bool = True) -> None:
        if count <= 0:
            return
        async with self._capacity_condition:
            self._reserved_deliveries = max(0, self._reserved_deliveries - count)
            if buffered:
                QUEUE_BUFFERED_DELIVERIES.dec(count)
            self._capacity_condition.notify_all()

    async def _run_delivery(self, delivery: _Delivery) -> None:
        try:
            if delivery.retry_entry is not None:
                await self._execute_retry_entry(delivery.retry_entry, delivery.stream)
            elif delivery.renew_stream_delivery:
                async with self._renew_stream_delivery(delivery.message, delivery.stream):
                    await self._process_message(
                        delivery.message,
                        delivery.stream,
                        raise_on_error=delivery.raise_on_error,
                        skip_ack=delivery.skip_ack,
                    )
            else:
                await self._process_message(
                    delivery.message,
                    delivery.stream,
                    raise_on_error=delivery.raise_on_error,
                    skip_ack=delivery.skip_ack,
                )
        except StreamDeliveryLostError as exc:
            logger.error(
                "Aborting %s after stream delivery loss: %s", delivery.message.event_id, exc
            )

    def _dispatch_reserved(self, delivery: _Delivery) -> asyncio.Task[None]:
        """Start a delivery whose capacity reservation is already held."""
        task = asyncio.create_task(
            self._run_delivery(delivery),
            name=f"process-{delivery.message.event_id}",
        )
        self._active_tasks.add(task)
        QUEUE_BUFFERED_DELIVERIES.dec()
        QUEUE_ACTIVE_DELIVERIES.inc()

        def forget(completed: asyncio.Task[None]) -> None:
            self._active_tasks.discard(completed)
            QUEUE_ACTIVE_DELIVERIES.dec()
            asyncio.create_task(self._release_capacity(buffered=False))
            if not completed.cancelled():
                with suppress(Exception):
                    error = completed.exception()
                    if error is not None:
                        logger.error(
                            "Unhandled queue delivery failure: %s",
                            error,
                            exc_info=(type(error), error, error.__traceback__),
                        )

        task.add_done_callback(forget)
        return task

    async def _ack_terminal_message(self, message: QueueMessage, stream: str) -> None:
        """Best-effort acknowledge a message after it has reached the DLQ."""
        try:
            await self._ack(stream, message.message_id)
        except Exception as ack_error:
            logger.warning(
                f"xack failed for terminal event {message.event_id}; "
                f"PEL entry may linger: {ack_error}"
            )

    @asynccontextmanager
    async def _local_ticket_lock(self, ticket_key: str) -> AsyncIterator[None]:
        """Serialize one local ticket and discard idle lock records promptly."""
        self._ticket_lock_users[ticket_key] += 1
        lock = self._ticket_locks[ticket_key]
        try:
            async with lock:
                yield
        finally:
            remaining = self._ticket_lock_users[ticket_key] - 1
            if remaining:
                self._ticket_lock_users[ticket_key] = remaining
            else:
                self._ticket_lock_users.pop(ticket_key, None)
                if not lock.locked():
                    self._ticket_locks.pop(ticket_key, None)

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

        if self._prepare_message is not None:
            try:
                prepared = await self._prepare_message(message)
                if prepared is None:
                    logger.info("Permanently ignoring unresolved event %s", message.event_id)
                    if not skip_ack:
                        await self._ack(stream, message.message_id)
                    return
                if (
                    prepared.message_id != message.message_id
                    or prepared.event_id != message.event_id
                    or prepared.source != message.source
                ):
                    raise ValueError(
                        "prepare_message must preserve stream message_id, event_id, and source"
                    )
                message = prepared
            except Exception as exc:
                logger.error("Failed to prepare event %s: %s", message.event_id, exc)
                if raise_on_error:
                    raise
                try:
                    queued = await self._retry_queue.enqueue_for_retry(
                        message,
                        str(exc),
                        stream=stream,
                        consumer_group=CONSUMER_GROUP,
                    )
                    if not queued:
                        await self._ack_terminal_message(message, stream)
                except Exception as retry_error:
                    logger.error(
                        "Failed to schedule preparation retry for %s: %s; delivery remains pending",
                        message.event_id,
                        retry_error,
                    )
                return

        if not message.ticket_key:
            error = ValueError(
                f"Event {message.event_id} has no canonical ticket key after preparation"
            )
            if raise_on_error:
                raise error
            try:
                queued = await self._retry_queue.enqueue_for_retry(
                    message,
                    str(error),
                    stream=stream,
                    consumer_group=CONSUMER_GROUP,
                )
                if not queued:
                    await self._ack_terminal_message(message, stream)
            except Exception as retry_error:
                logger.error(
                    "Failed to schedule unresolved event %s: %s", message.event_id, retry_error
                )
            return

        # The local lock preserves arrival order within this process. The Redis
        # lease prevents another worker from handling the same ticket at once.
        try:
            async with (
                self._local_ticket_lock(message.ticket_key),
                self._semaphore,
                self._distributed_ticket_lock(message.ticket_key),
            ):
                await self._handle_locked_message(
                    message, stream, handler, raise_on_error, skip_ack
                )
        except TicketLockLostError as exc:
            logger.error("Aborting %s after ticket lock loss: %s", message.event_id, exc)
            if raise_on_error:
                raise
            try:
                moved_to_dlq = not await self._retry_queue.enqueue_for_retry(
                    message,
                    str(exc),
                    stream=stream,
                    consumer_group=CONSUMER_GROUP,
                )
                if moved_to_dlq:
                    await self._ack_terminal_message(message, stream)
            except Exception as retry_err:
                logger.error(
                    f"Failed to enqueue {message.event_id} for retry after ticket lock loss: "
                    f"{retry_err}. Message remains in PEL for reclaim."
                )

    async def _handle_locked_message(
        self,
        message: QueueMessage,
        stream: str,
        handler: MessageHandler,
        raise_on_error: bool,
        skip_ack: bool,
    ) -> None:
        """Process a message while its local and distributed locks are held."""
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
                moved_to_dlq = not await self._retry_queue.enqueue_for_retry(
                    message,
                    str(e),
                    stream=stream,
                    consumer_group=CONSUMER_GROUP,
                )
                if moved_to_dlq:
                    await self._ack_terminal_message(message, stream)
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
            reservation = await self._reserve_capacity(10, recovery=False)
            if reservation == 0:
                break
            transferred = 0
            try:
                # Read from consumer group
                messages = await redis_client.xreadgroup(
                    CONSUMER_GROUP,
                    self.consumer_name,
                    {stream: ">"},
                    count=reservation,
                    block=1000,
                )
                for _stream_name, entries in messages:
                    for message_id, data in entries:
                        try:
                            message = QueueMessage.from_redis(message_id, data)
                        except Exception as exc:
                            logger.exception("Malformed stream message %s: %s", message_id, exc)
                            await self._quarantine_malformed(stream, message_id, data, str(exc))
                            continue
                        self._dispatch_reserved(
                            _Delivery(message, stream, renew_stream_delivery=True)
                        )
                        transferred += 1
                await self._release_capacity(reservation - transferred)

            except asyncio.CancelledError:
                await self._release_capacity(reservation - transferred)
                break
            except Exception as e:
                await self._release_capacity(reservation - transferred)
                logger.error(f"Error consuming from {stream}: {e}")
                await asyncio.sleep(1)  # Brief pause before retry

        # Drain any in-flight processing tasks before returning
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def _quarantine_malformed(
        self,
        stream: str,
        message_id: str,
        data: dict[str, Any],
        error: str,
    ) -> None:
        """Persist evidence for malformed deliveries before clearing their PEL entry."""
        redis_client = await self._get_redis()
        key = f"forge:queue:malformed:{stream}:{message_id}"
        await redis_client.hset(
            key,
            mapping={"error": error, "fields": json.dumps(data, default=str)},
        )
        await redis_client.expire(key, 30 * 24 * 60 * 60)
        await self._ack(stream, message_id)

    @asynccontextmanager
    async def _renew_retry_claim(self, entry: RetryEntry) -> AsyncIterator[None]:
        """Renew a retry lease and abort processing if ownership is lost."""
        if entry.lease_until is None:
            yield
            return

        owner_task = asyncio.current_task()
        lease_lost = asyncio.Event()
        lease_deadline = time.monotonic() + max(
            0.01,
            RETRY_CLAIM_LEASE_SECONDS - TICKET_LOCK_EXPIRY_SAFETY_SECONDS,
        )

        async def renew() -> None:
            nonlocal lease_deadline
            while True:
                await asyncio.sleep(
                    min(RETRY_CLAIM_RENEW_SECONDS, max(0.001, lease_deadline - time.monotonic()))
                )
                if time.monotonic() >= lease_deadline:
                    lease_lost.set()
                    QUEUE_LEASE_LOSSES.labels(kind="retry").inc()
                    if owner_task is not None:
                        owner_task.cancel()
                    return
                try:
                    renewed = await asyncio.wait_for(
                        self._retry_queue.renew_retry_claim(entry),
                        timeout=min(5, max(0.001, lease_deadline - time.monotonic())),
                    )
                    if not renewed:
                        lease_lost.set()
                        QUEUE_LEASE_LOSSES.labels(kind="retry").inc()
                        if owner_task is not None:
                            owner_task.cancel()
                        return
                    lease_deadline = time.monotonic() + max(
                        0.01,
                        RETRY_CLAIM_LEASE_SECONDS - TICKET_LOCK_EXPIRY_SAFETY_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to renew retry lease for %s", entry.message.event_id)
                    if time.monotonic() >= lease_deadline:
                        lease_lost.set()
                        QUEUE_LEASE_LOSSES.labels(kind="retry").inc()
                        if owner_task is not None:
                            owner_task.cancel()
                        return

        renewal_task = asyncio.create_task(renew(), name=f"renew-retry-{entry.message.event_id}")
        try:
            yield
        except asyncio.CancelledError:
            if lease_lost.is_set():
                raise RetryLeaseLostError(
                    f"Lost retry lease for {entry.message.event_id}"
                ) from None
            raise
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task

    async def _process_due_retries_once(self) -> None:
        """Dispatch one batch of due retries.

        Kept separate from the polling loop so recovery and terminal DLQ
        behavior can be exercised deterministically in integration tests.
        """
        reservation = await self._reserve_capacity(10, recovery=True, allow_stopped=True)
        if reservation == 0:
            return
        try:
            entries = await self._retry_queue.claim_due_messages(limit=reservation)
        except Exception:
            await self._release_capacity(reservation)
            raise
        if len(entries) > reservation:
            # A broken/mocked retry backend must not strand capacity or leave
            # leases hidden while the dispatcher refuses the over-sized batch.
            await self._release_capacity(reservation)
            raise RuntimeError("retry queue claimed more deliveries than reserved capacity")
        await self._release_capacity(reservation - len(entries))
        tasks: list[asyncio.Task[None]] = []
        for entry in entries:
            retry_stream = (
                JIRA_STREAM if entry.message.source == EventSource.JIRA else SOURCE_CONTROL_STREAM
            )
            tasks.append(
                self._dispatch_reserved(_Delivery(entry.message, retry_stream, retry_entry=entry))
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_retry_entry(self, entry: RetryEntry, retry_stream: str) -> None:
        """Run one leased retry and durably complete or reschedule it."""
        try:
            async with self._renew_retry_claim(entry):
                await self._process_message(
                    entry.message, retry_stream, raise_on_error=True, skip_ack=True
                )
        except RetryLeaseLostError:
            logger.error(
                "Stopped retry processing after losing ownership of %s", entry.message.event_id
            )
            return
        except Exception as exc:
            logger.warning(
                "Retry attempt %s failed for %s:%s: %s",
                entry.attempt,
                entry.message.ticket_key,
                entry.message.event_id,
                exc,
            )
            queued = await self._retry_queue.reschedule_retry(
                entry,
                str(exc),
                stream=retry_stream,
                consumer_group=CONSUMER_GROUP,
            )
            if not queued:
                logger.info(
                    "Retry %s was terminal or no longer owned; durable transition handled by Redis",
                    entry.message.message_id,
                )
            return

        completed = await self._retry_queue.complete_retry(
            entry,
            stream=retry_stream,
            consumer_group=CONSUMER_GROUP,
        )
        if not completed:
            logger.warning("Stale retry completion rejected for %s", entry.message.message_id)
            return
        logger.info(
            "Retry succeeded for %s:%s (attempt %s)",
            entry.message.ticket_key,
            entry.message.event_id,
            entry.attempt,
        )

    async def _process_terminal_notifications_once(self) -> None:
        """Deliver one leased batch from the terminal-notification outbox."""
        if self._terminal_failure_handler is None:
            return

        entries = await self._retry_queue.claim_due_terminal_notifications()
        for entry in entries:
            try:
                await self._terminal_failure_handler(entry.message, entry.error)
            except Exception as exc:
                logger.error(
                    "Terminal failure callback failed for %s: %s",
                    entry.message.event_id,
                    exc,
                )
                continue
            await self._retry_queue.remove_terminal_notification(entry)

    async def _process_retry_queue(self) -> None:
        """Poll the retry queue and re-dispatch due messages."""
        while self._running:
            try:
                for stream in self._handled_streams():
                    await self._recover_pending_once(stream)
                await self._process_due_retries_once()
                await self._process_terminal_notifications_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in retry queue poller: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _handled_streams(self) -> list[str]:
        streams = []
        if EventSource.JIRA in self._handlers:
            streams.append(JIRA_STREAM)
        if EventSource.SOURCE_CONTROL in self._handlers:
            streams.append(SOURCE_CONTROL_STREAM)
        return streams

    async def _recover_pending_once(self, stream: str) -> int:
        """Claim a bounded batch of stale PEL entries and redispatch normally."""
        if not hasattr(await self._get_redis(), "xautoclaim"):
            logger.warning("Redis client lacks XAUTOCLAIM; pending recovery disabled")
            return 0
        reservation = await self._reserve_capacity(10, recovery=True)
        if reservation == 0:
            return 0
        redis_client = await self._get_redis()
        cursor = self._pending_scan_cursors.get(stream, "0-0")
        try:
            result = await redis_client.xautoclaim(
                stream,
                CONSUMER_GROUP,
                self.consumer_name,
                self._pending_idle_ms,
                cursor,
                count=reservation,
            )
        except Exception:
            await self._release_capacity(reservation)
            logger.exception("Failed to scan pending deliveries for %s", stream)
            return 0

        next_cursor, entries = result[0], result[1]
        QUEUE_RECLAIMED_DELIVERIES.inc(len(entries))
        self._pending_scan_cursors[stream] = (
            next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor)
        )
        transferred = 0
        try:
            for message_id, fields in entries:
                try:
                    message = QueueMessage.from_redis(message_id, fields)
                except Exception as exc:
                    logger.exception("Malformed recovered message %s: %s", message_id, exc)
                    await self._quarantine_malformed(stream, message_id, fields, str(exc))
                    continue

                # A durable retry record owns scheduled/active retry work. Clear
                # this PEL entry instead of bypassing its backoff or duplicating it.
                if await self._retry_queue.has_retry(message):
                    await self._ack(stream, message.message_id)
                    continue
                self._dispatch_reserved(_Delivery(message, stream, renew_stream_delivery=True))
                transferred += 1
        except Exception:
            await self._release_capacity(reservation - transferred)
            logger.exception("Could not dispatch recovered deliveries from %s", stream)
            return transferred

        await self._release_capacity(reservation - transferred)
        return transferred

    @asynccontextmanager
    async def _renew_stream_delivery(
        self, message: QueueMessage, stream: str
    ) -> AsyncIterator[None]:
        """Refresh a live PEL entry so pending recovery cannot duplicate it."""
        redis_client = await self._get_redis()
        owner_task = asyncio.current_task()
        lease_lost = asyncio.Event()
        lease_seconds = self._pending_idle_ms / 1000
        safety_seconds = min(1.0, lease_seconds / 3)
        last_renewed = time.monotonic()
        deadline = last_renewed + max(0.01, lease_seconds - safety_seconds)

        async def renew() -> None:
            nonlocal last_renewed, deadline
            while True:
                await asyncio.sleep(
                    min(
                        STREAM_DELIVERY_RENEW_SECONDS,
                        max(0.01, lease_seconds / 3),
                        max(0.001, deadline - time.monotonic()),
                    )
                )
                if time.monotonic() >= deadline:
                    lease_lost.set()
                    QUEUE_LEASE_LOSSES.labels(kind="stream_delivery").inc()
                    if owner_task is not None:
                        owner_task.cancel()
                    return
                try:
                    renewed = await asyncio.wait_for(
                        redis_client.eval(
                            _RENEW_PENDING_DELIVERY_SCRIPT,
                            1,
                            stream,
                            CONSUMER_GROUP,
                            self.consumer_name,
                            message.message_id,
                        ),
                        timeout=min(5, max(0.001, deadline - time.monotonic())),
                    )
                    if int(renewed) != 1:
                        lease_lost.set()
                        QUEUE_LEASE_LOSSES.labels(kind="stream_delivery").inc()
                        if owner_task is not None:
                            owner_task.cancel()
                        return
                    last_renewed = time.monotonic()
                    deadline = last_renewed + max(0.01, lease_seconds - safety_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to renew stream delivery %s", message.message_id)
                    if time.monotonic() >= deadline:
                        lease_lost.set()
                        QUEUE_LEASE_LOSSES.labels(kind="stream_delivery").inc()
                        if owner_task is not None:
                            owner_task.cancel()
                        return

        renewal = asyncio.create_task(renew(), name=f"renew-delivery-{message.message_id}")
        try:
            yield
        except asyncio.CancelledError:
            if lease_lost.is_set():
                raise StreamDeliveryLostError(
                    f"Lost stream delivery {stream}/{message.message_id}"
                ) from None
            raise
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

    async def start(self) -> None:
        """Start consuming from all registered streams."""
        await self._ensure_consumer_groups()
        self._running = True
        streams = self._handled_streams()
        for stream in streams:
            await self._recover_pending_once(stream)

        coroutines = [
            self._consume_stream(
                stream,
                EventSource.JIRA if stream == JIRA_STREAM else EventSource.SOURCE_CONTROL,
            )
            for stream in streams
        ]
        if coroutines:
            coroutines.append(self._process_retry_queue())
            self._background_tasks = {
                asyncio.create_task(coro, name=f"queue-{self.consumer_name}-{index}")
                for index, coro in enumerate(coroutines)
            }
            logger.info("Consumer %s starting...", self.consumer_name)
            try:
                await asyncio.gather(*self._background_tasks)
            finally:
                self._background_tasks.clear()

    async def stop(self) -> None:
        """Stop consuming messages and drain all in-flight tasks.

        Stop reading first, then drain for a bounded interval. If that expires,
        cancel active owners and leave their unacknowledged deliveries in the PEL.
        """
        self._running = False
        async with self._capacity_condition:
            self._capacity_condition.notify_all()
        if self._active_tasks:
            pending = set(self._active_tasks)
            _done, pending = await asyncio.wait(
                pending,
                timeout=self._shutdown_timeout_seconds,
            )
            if pending:
                logger.warning(
                    "Shutdown drain expired with %d active deliveries; cancelling them "
                    "without acknowledging unfinished work",
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        logger.info(f"Consumer {self.consumer_name} stopped")
