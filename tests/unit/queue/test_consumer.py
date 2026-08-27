"""Unit tests for QueueConsumer — fire-and-forget concurrency fix (AISOS-709).

All tests use pytest-asyncio and mock Redis via unittest.mock.AsyncMock.
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import forge.queue.consumer as consumer_module
from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRedisLock:
    """Small in-memory stand-in that shares ownership by Redis key."""

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self.extend_calls = 0

    async def acquire(self) -> bool:
        await self._lock.acquire()
        return True

    async def extend(self, _seconds: int, *, replace_ttl: bool) -> bool:
        assert replace_ttl is True
        self.extend_calls += 1
        return True

    async def release(self) -> None:
        self._lock.release()


class _LostLeaseRedisLock(_FakeRedisLock):
    """Redis lock stand-in whose first renewal reports lost ownership."""

    async def extend(self, _seconds: int, *, replace_ttl: bool) -> bool:
        assert replace_ttl is True
        self.extend_calls += 1
        return False


def _make_message(
    ticket_key: str,
    message_id: str = "1234567890-0",
    event_id: str | None = None,
    source: EventSource = EventSource.JIRA,
) -> QueueMessage:
    """Build a minimal QueueMessage for testing."""
    return QueueMessage(
        message_id=message_id,
        event_id=event_id or f"evt-{message_id}",
        source=source,
        event_type="jira:issue_updated",
        ticket_key=ticket_key,
        payload={},
        timestamp=datetime.utcnow(),
    )


def _make_consumer(redis_mock: MagicMock, max_tasks: int = 20) -> QueueConsumer:
    """Return a QueueConsumer wired to a mock Redis client and a mock RetryQueue."""
    from forge.queue.retry import RetryQueue

    consumer = QueueConsumer(
        consumer_name="test-worker",
        redis_client=redis_mock,
        max_concurrent_tasks=max_tasks,
    )
    # Replace the real RetryQueue with a mock so tests do not need a live Redis
    # connection when the handler fails and the consumer attempts to enqueue
    # the message for retry.
    retry_mock = MagicMock(spec=RetryQueue)
    retry_mock.enqueue_for_retry = AsyncMock(return_value=True)  # queued, not DLQ
    retry_mock.claim_due_messages = AsyncMock(return_value=[])
    retry_mock.remove_from_retry = AsyncMock()
    retry_mock.remove_from_retry_without_counter_reset = AsyncMock()
    consumer._retry_queue = retry_mock
    return consumer


def _make_redis_mock() -> MagicMock:
    """Return a mock Redis client with sensible async defaults."""
    mock = MagicMock()
    locks: dict[str, asyncio.Lock] = {}
    created_locks: list[_FakeRedisLock] = []

    def make_lock(name: str, **_kwargs) -> _FakeRedisLock:
        lock = _FakeRedisLock(locks.setdefault(name, asyncio.Lock()))
        created_locks.append(lock)
        return lock

    mock.lock = MagicMock(side_effect=make_lock)
    mock.created_locks = created_locks
    mock.xack = AsyncMock(return_value=1)
    mock.xgroup_create = AsyncMock()
    mock.xreadgroup = AsyncMock(return_value=[])
    return mock


# ---------------------------------------------------------------------------
# Test: concurrent dispatch for different ticket keys (AISOS-709 regression)
# ---------------------------------------------------------------------------


class TestConcurrentDispatch:
    """Two messages with different ticket keys run concurrently."""

    @pytest.mark.asyncio
    async def test_different_tickets_processed_concurrently(self) -> None:
        """Total wall-clock time < 350 ms proves concurrent (not sequential) execution.

        Each handler sleeps 200 ms; sequential execution would take ≥ 400 ms.
        """
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        entry_times: dict[str, float] = {}
        exit_times: dict[str, float] = {}

        async def handler(message: QueueMessage) -> None:
            entry_times[message.ticket_key] = time.monotonic()
            await asyncio.sleep(0.2)
            exit_times[message.ticket_key] = time.monotonic()

        consumer.register_handler(EventSource.JIRA, handler)

        msg_a = _make_message("TICKET-A", message_id="1-0")
        msg_b = _make_message("TICKET-B", message_id="2-0")
        stream = "jira-events"

        start = time.monotonic()
        task_a = asyncio.create_task(consumer._process_message(msg_a, stream))
        task_b = asyncio.create_task(consumer._process_message(msg_b, stream))
        await asyncio.gather(task_a, task_b)
        elapsed = time.monotonic() - start

        assert elapsed < 0.35, (
            f"Expected concurrent execution (< 350 ms) but took {elapsed * 1000:.0f} ms. "
            "Messages for different tickets must run concurrently."
        )
        # Both tickets must have been processed
        assert "TICKET-A" in exit_times
        assert "TICKET-B" in exit_times


# ---------------------------------------------------------------------------
# Test: FIFO ordering for same ticket key
# ---------------------------------------------------------------------------


class TestFifoOrdering:
    """Messages for the same ticket key are serialised in order."""

    @pytest.mark.asyncio
    async def test_same_ticket_processes_in_fifo_order(self) -> None:
        """Handler invocations for the same ticket key must be ordered [0, 1]."""
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        order: list[int] = []

        async def make_handler(index: int):
            async def handler(_message: QueueMessage) -> None:
                order.append(index)
                await asyncio.sleep(0.05)

            return handler

        # Use a single handler that appends a counter we embed in the message
        call_order: list[int] = []

        async def recording_handler(message: QueueMessage) -> None:
            idx = int(message.event_id.split("-")[1])
            call_order.append(idx)
            await asyncio.sleep(0.05)

        consumer.register_handler(EventSource.JIRA, recording_handler)

        stream = "jira-events"
        msg_0 = _make_message("SAME-TICKET", message_id="1-0", event_id="evt-0")
        msg_1 = _make_message("SAME-TICKET", message_id="2-0", event_id="evt-1")

        # Fire both tasks simultaneously
        task_0 = asyncio.create_task(consumer._process_message(msg_0, stream))
        task_1 = asyncio.create_task(consumer._process_message(msg_1, stream))
        await asyncio.gather(task_0, task_1)

        assert call_order == [0, 1], (
            f"Expected FIFO order [0, 1] but got {call_order}. "
            "Per-ticket locks must serialise same-ticket events."
        )

    @pytest.mark.asyncio
    async def test_same_ticket_is_serialized_across_consumer_instances(self) -> None:
        """Independent worker instances must share the Redis ticket lock."""
        redis_mock = _make_redis_mock()
        worker_a = _make_consumer(redis_mock)
        worker_b = _make_consumer(redis_mock)

        first_started = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        peak_active = 0

        async def handler(message: QueueMessage) -> None:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            if message.event_id == "evt-first":
                first_started.set()
                await release_first.wait()
            active -= 1

        worker_a.register_handler(EventSource.JIRA, handler)
        worker_b.register_handler(EventSource.JIRA, handler)
        first = _make_message("SAME-TICKET", message_id="1-0", event_id="evt-first")
        second = _make_message("SAME-TICKET", message_id="2-0", event_id="evt-second")

        first_task = asyncio.create_task(worker_a._process_message(first, "jira-events"))
        await first_started.wait()
        second_task = asyncio.create_task(worker_b._process_message(second, "jira-events"))
        await asyncio.sleep(0.02)

        assert peak_active == 1
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert peak_active == 1

        lock_name = "forge:queue:ticket-lock:SAME-TICKET"
        assert [call.args[0] for call in redis_mock.lock.call_args_list] == [
            lock_name,
            lock_name,
        ]

    @pytest.mark.asyncio
    async def test_distributed_lock_lease_is_renewed(self, monkeypatch) -> None:
        """A long-running handler retains ownership beyond the initial lease."""
        monkeypatch.setattr(consumer_module, "TICKET_LOCK_RENEW_SECONDS", 0.001)
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        async with consumer._distributed_ticket_lock("LONG-TICKET"):
            await asyncio.sleep(0.01)

        assert redis_mock.created_locks[0].extend_calls > 0

    @pytest.mark.asyncio
    async def test_lost_distributed_lock_aborts_handler_and_retries(self, monkeypatch) -> None:
        """A handler must not continue after its distributed lease is lost."""
        monkeypatch.setattr(consumer_module, "TICKET_LOCK_RENEW_SECONDS", 0.001)
        redis_mock = _make_redis_mock()
        lost_lock = _LostLeaseRedisLock(asyncio.Lock())
        redis_mock.lock = MagicMock(return_value=lost_lock)
        consumer = _make_consumer(redis_mock)
        handler_started = asyncio.Event()
        handler_stopped = asyncio.Event()

        async def handler(_message: QueueMessage) -> None:
            handler_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                handler_stopped.set()

        consumer.register_handler(EventSource.JIRA, handler)
        message = _make_message("LOST-LEASE", event_id="evt-lost-lease")

        await asyncio.wait_for(consumer._process_message(message, "jira-events"), timeout=1)

        assert handler_started.is_set()
        assert handler_stopped.is_set()
        assert lost_lock.extend_calls == 1
        consumer._retry_queue.enqueue_for_retry.assert_awaited_once()
        retry_message, retry_error = consumer._retry_queue.enqueue_for_retry.call_args.args
        assert retry_message is message
        assert "Lost distributed ticket lock" in retry_error
        redis_mock.xack.assert_not_called()


# ---------------------------------------------------------------------------
# Test: xack behaviour — success vs failure
# ---------------------------------------------------------------------------


class TestXackBehaviour:
    """xack is called only when the handler succeeds."""

    @pytest.mark.asyncio
    async def test_xack_called_on_success_not_on_failure(self) -> None:
        """xack must be called for the successful message only."""
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        msg_0 = _make_message("TICKET-OK", message_id="1-0", event_id="evt-ok")
        msg_1 = _make_message("TICKET-FAIL", message_id="2-0", event_id="evt-fail")
        stream = "jira-events"

        async def handler(message: QueueMessage) -> None:
            if message.message_id == "2-0":
                raise RuntimeError("deliberate failure")

        consumer.register_handler(EventSource.JIRA, handler)

        task_0 = asyncio.create_task(consumer._process_message(msg_0, stream))
        task_1 = asyncio.create_task(consumer._process_message(msg_1, stream))
        await asyncio.gather(task_0, task_1)

        # xack should have been called exactly once — for msg_0
        redis_mock.xack.assert_called_once_with(stream, CONSUMER_GROUP, "1-0")

    @pytest.mark.asyncio
    async def test_xack_not_called_on_handler_failure(self) -> None:
        """xack must never be called when the handler raises."""
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        async def failing_handler(_message: QueueMessage) -> None:
            raise ValueError("boom")

        consumer.register_handler(EventSource.JIRA, failing_handler)

        msg = _make_message("TICKET-X", message_id="5-0")
        await consumer._process_message(msg, "jira-events")

        redis_mock.xack.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_exception_propagates_from_failing_handler(self) -> None:
        """A handler failure must not propagate out of _process_message."""
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        async def failing_handler(_message: QueueMessage) -> None:
            raise ValueError("boom")

        consumer.register_handler(EventSource.JIRA, failing_handler)

        msg = _make_message("TICKET-Y", message_id="6-0")
        # Must not raise
        await consumer._process_message(msg, "jira-events")
        redis_mock.xack.assert_not_called()


# ---------------------------------------------------------------------------
# Test: semaphore caps peak concurrency
# ---------------------------------------------------------------------------


class TestSemaphoreConcurrencyLimit:
    """At most MAX_CONCURRENT_TASKS handlers run simultaneously."""

    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrent_handlers(self) -> None:
        """With MAX_CONCURRENT_TASKS=3, at most 3 handlers run at once.

        A 4th message must wait until one of the first three finishes.
        """
        cap = 3
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock, max_tasks=cap)

        active = 0
        peak_active = 0
        gate = asyncio.Event()

        async def blocking_handler(_message: QueueMessage) -> None:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await gate.wait()  # Block until released
            active -= 1

        consumer.register_handler(EventSource.JIRA, blocking_handler)

        stream = "jira-events"
        messages = [_make_message(f"TICKET-{i}", message_id=f"{i}-0") for i in range(4)]

        tasks = [asyncio.create_task(consumer._process_message(msg, stream)) for msg in messages]

        # Give the first cap tasks time to acquire the semaphore and block
        await asyncio.sleep(0.05)

        # Peak concurrency must not exceed the cap
        assert peak_active <= cap, f"Expected ≤ {cap} concurrent handlers but saw {peak_active}."

        # Release all blocked handlers
        gate.set()
        await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Test: stop() drains in-flight tasks
# ---------------------------------------------------------------------------


class TestStopDrainsInflightTasks:
    """stop() must wait for all dispatched tasks to complete."""

    @pytest.mark.asyncio
    async def test_stop_waits_for_inflight_tasks(self) -> None:
        """stop() must return only after the in-flight handler finishes."""
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)
        consumer._running = True

        completion_time: float | None = None

        async def slow_handler(_message: QueueMessage) -> None:
            nonlocal completion_time
            await asyncio.sleep(0.1)
            completion_time = time.monotonic()

        consumer.register_handler(EventSource.JIRA, slow_handler)

        msg = _make_message("TICKET-SLOW", message_id="9-0")
        stream = "jira-events"

        # Dispatch the task and add it to _active_tasks (as _consume_stream would)
        task = asyncio.create_task(consumer._process_message(msg, stream))
        consumer._active_tasks.add(task)
        task.add_done_callback(consumer._active_tasks.discard)

        stop_return_time = None

        async def do_stop() -> None:
            nonlocal stop_return_time
            await consumer.stop()
            stop_return_time = time.monotonic()

        await do_stop()

        assert completion_time is not None, "Handler never completed"
        assert stop_return_time is not None, "stop() never returned"
        assert completion_time <= stop_return_time, (
            "stop() returned before the in-flight task finished — messages may be un-acked."
        )


# ---------------------------------------------------------------------------
# Test: AISOS-709 regression — slow ticket does not block fast ticket
# ---------------------------------------------------------------------------


class TestAISOS709Regression:
    """Direct regression test: slow ticket must not block fast ticket."""

    @pytest.mark.asyncio
    async def test_slow_ticket_does_not_block_fast_ticket(self) -> None:
        """Ticket B (fast) must complete before Ticket A (slow) finishes.

        Ticket A handler sleeps 500 ms; Ticket B handler returns immediately.
        Both messages are dispatched concurrently (as fire-and-forget tasks).
        If blocking were still present, B would not finish until A completes.
        """
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        completion_times: dict[str, float] = {}

        async def handler(message: QueueMessage) -> None:
            if message.ticket_key == "TICKET-A":
                await asyncio.sleep(0.5)
            completion_times[message.ticket_key] = time.monotonic()

        consumer.register_handler(EventSource.JIRA, handler)

        msg_a = _make_message("TICKET-A", message_id="10-0")
        msg_b = _make_message("TICKET-B", message_id="11-0")
        stream = "jira-events"

        # Dispatch both concurrently, just as _consume_stream does
        task_a = asyncio.create_task(consumer._process_message(msg_a, stream))
        task_b = asyncio.create_task(consumer._process_message(msg_b, stream))
        await asyncio.gather(task_a, task_b)

        assert "TICKET-A" in completion_times, "TICKET-A never processed"
        assert "TICKET-B" in completion_times, "TICKET-B never processed"

        assert completion_times["TICKET-B"] < completion_times["TICKET-A"], (
            "TICKET-B (fast) should have completed before TICKET-A (slow). "
            "This is the AISOS-709 regression — sequential processing detected."
        )


class TestLegacyStreamMigration:
    """The pre-rename source-control stream is never auto-consumed.

    Its entries predate the NormalizedEvent/adapter cutover and have no
    normalized_event to deserialize, so processing them would silently ack
    away whatever CI/review/merge signal they carried instead of acting on
    it. They're left for a deliberate, out-of-band migration; only
    forge:events:source_control (and forge:events:jira) are consumed live.
    """

    @pytest.mark.asyncio
    async def test_ensure_consumer_groups_does_not_include_legacy_stream(self) -> None:
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)

        await consumer._ensure_consumer_groups()

        created_streams = {call.args[0] for call in redis_mock.xgroup_create.await_args_list}
        assert "forge:events:github" not in created_streams
        assert "forge:events:source_control" in created_streams

    @pytest.mark.asyncio
    async def test_start_does_not_consume_legacy_stream(self) -> None:
        redis_mock = _make_redis_mock()
        consumer = _make_consumer(redis_mock)
        consumer.register_handler(EventSource.SOURCE_CONTROL, AsyncMock())

        consumed_streams: list[str] = []

        async def fake_consume_stream(stream: str, _source: EventSource) -> None:
            consumed_streams.append(stream)

        consumer._consume_stream = fake_consume_stream
        consumer._process_retry_queue = AsyncMock()

        await consumer.start()

        assert "forge:events:source_control" in consumed_streams
        assert "forge:events:github" not in consumed_streams
