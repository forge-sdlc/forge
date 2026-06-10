"""Unit tests for QueueConsumer — fire-and-forget concurrency fix (AISOS-709).

All tests use pytest-asyncio and mock Redis via unittest.mock.AsyncMock.
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    retry_mock.get_due_messages = AsyncMock(return_value=[])
    retry_mock.remove_from_retry = AsyncMock()
    retry_mock.remove_from_retry_without_counter_reset = AsyncMock()
    consumer._retry_queue = retry_mock
    return consumer


def _make_redis_mock() -> MagicMock:
    """Return a mock Redis client with sensible async defaults."""
    mock = MagicMock()
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
            "Per-ticket asyncio.Lock must serialise same-ticket events."
        )


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
