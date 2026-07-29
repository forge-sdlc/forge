"""Unit tests for QueueConsumer retry integration."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage
from forge.queue.retry import RetryEntry, RetryQueue, TerminalNotification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoopRedisLock:
    async def acquire(self) -> bool:
        return True

    async def extend(self, _seconds: int, *, replace_ttl: bool) -> bool:
        return replace_ttl

    async def release(self) -> None:
        return None


def configure_redis_mock(redis_mock: AsyncMock) -> None:
    """Make the async Redis mock expose its synchronous lock factory."""
    redis_mock.lock = MagicMock(side_effect=lambda *_args, **_kwargs: _NoopRedisLock())


def make_message(
    event_id: str = "evt-001",
    ticket_key: str = "TEST-123",
    source: EventSource = EventSource.JIRA,
) -> QueueMessage:
    return QueueMessage(
        message_id="1-0",
        event_id=event_id,
        source=source,
        event_type="issue_updated",
        ticket_key=ticket_key,
    )


def make_consumer() -> QueueConsumer:
    """Return a QueueConsumer with a mocked RetryQueue."""
    redis_mock = AsyncMock()
    configure_redis_mock(redis_mock)
    redis_mock.xack = AsyncMock(return_value=1)
    consumer = QueueConsumer(consumer_name="test-worker", redis_client=redis_mock)
    consumer._running = True
    # Replace the real RetryQueue with a mock
    consumer._retry_queue = MagicMock(spec=RetryQueue)
    consumer._retry_queue.enqueue_for_retry = AsyncMock(return_value=True)
    consumer._retry_queue.claim_due_messages = AsyncMock(return_value=[])
    consumer._retry_queue.renew_retry_claim = AsyncMock(return_value=True)
    consumer._retry_queue.remove_from_retry = AsyncMock()
    consumer._retry_queue.remove_from_retry_without_counter_reset = AsyncMock()
    consumer._retry_queue.claim_due_terminal_notifications = AsyncMock(return_value=[])
    consumer._retry_queue.remove_terminal_notification = AsyncMock()
    return consumer


# ---------------------------------------------------------------------------
# RetryQueue is wired into QueueConsumer
# ---------------------------------------------------------------------------


class TestQueueConsumerInit:
    def test_retry_queue_attached(self):
        consumer = QueueConsumer(consumer_name="worker-1")
        assert isinstance(consumer._retry_queue, RetryQueue)


# ---------------------------------------------------------------------------
# _consume_stream — success path
# ---------------------------------------------------------------------------


class TestConsumeStreamSuccess:
    @pytest.mark.asyncio
    async def test_xack_on_success(self):
        """Successful processing must call xack."""
        consumer = make_consumer()
        message = make_message()

        call_count = 0

        async def xreadgroup_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("stream:jira", [("1-0", message.to_dict())])]
            consumer._running = False
            return []

        redis_mock = AsyncMock()
        configure_redis_mock(redis_mock)
        redis_mock.xreadgroup = AsyncMock(side_effect=xreadgroup_side_effect)
        redis_mock.xack = AsyncMock()
        consumer._redis = redis_mock

        consumer.register_handler(EventSource.JIRA, AsyncMock())

        await consumer._consume_stream("stream:jira", EventSource.JIRA)

        redis_mock.xack.assert_called_once_with("stream:jira", CONSUMER_GROUP, "1-0")
        consumer._retry_queue.enqueue_for_retry.assert_not_called()


# ---------------------------------------------------------------------------
# _consume_stream — failure path (retry enqueued)
# ---------------------------------------------------------------------------


class TestConsumeStreamFailure:
    @pytest.mark.asyncio
    async def test_enqueues_for_retry_on_failure(self):
        """On handler failure, the message is enqueued for retry (not xacked)."""
        consumer = make_consumer()
        message = make_message()

        call_count = 0

        async def xreadgroup_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("stream:jira", [("1-0", message.to_dict())])]
            consumer._running = False
            return []

        redis_mock = AsyncMock()
        configure_redis_mock(redis_mock)
        redis_mock.xreadgroup = AsyncMock(side_effect=xreadgroup_side_effect)
        redis_mock.xack = AsyncMock()
        consumer._redis = redis_mock

        failing_handler = AsyncMock(side_effect=RuntimeError("boom"))
        consumer.register_handler(EventSource.JIRA, failing_handler)

        await consumer._consume_stream("stream:jira", EventSource.JIRA)

        # enqueue_for_retry must be called with the message and the error string
        consumer._retry_queue.enqueue_for_retry.assert_awaited_once()
        call_args = consumer._retry_queue.enqueue_for_retry.call_args[0]
        assert call_args[0].event_id == "evt-001"
        assert "boom" in call_args[1]

        # xack must NOT have been called (message stays in PEL awaiting retry)
        redis_mock.xack.assert_not_called()

    @pytest.mark.asyncio
    async def test_xack_after_dlq_move(self):
        """When enqueue_for_retry returns False (DLQ move), xack clears the PEL entry."""
        consumer = make_consumer()
        consumer._retry_queue.enqueue_for_retry = AsyncMock(return_value=False)

        message = make_message()

        call_count = 0

        async def xreadgroup_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("stream:jira", [("1-0", message.to_dict())])]
            consumer._running = False
            return []

        redis_mock = AsyncMock()
        configure_redis_mock(redis_mock)
        redis_mock.xreadgroup = AsyncMock(side_effect=xreadgroup_side_effect)
        redis_mock.xack = AsyncMock()
        consumer._redis = redis_mock

        failing_handler = AsyncMock(side_effect=RuntimeError("permanent failure"))
        consumer.register_handler(EventSource.JIRA, failing_handler)

        await consumer._consume_stream("stream:jira", EventSource.JIRA)

        # xack must be called to clear PEL after DLQ move
        redis_mock.xack.assert_called_once_with("stream:jira", CONSUMER_GROUP, "1-0")

    @pytest.mark.asyncio
    async def test_dlq_ack_does_not_wait_for_terminal_callback(self):
        """Durable notification delivery is independent from stream acknowledgement."""
        callback = AsyncMock(side_effect=RuntimeError("Jira unavailable"))
        consumer = QueueConsumer("test-worker", terminal_failure_handler=callback)
        consumer._retry_queue = MagicMock(spec=RetryQueue)
        consumer._retry_queue.enqueue_for_retry = AsyncMock(return_value=False)

        redis_mock = AsyncMock()
        configure_redis_mock(redis_mock)
        redis_mock.xack = AsyncMock()
        consumer._redis = redis_mock
        consumer.register_handler(
            EventSource.JIRA,
            AsyncMock(side_effect=RuntimeError("permanent failure")),
        )

        await consumer._process_message(make_message(), "stream:jira")

        callback.assert_not_awaited()
        redis_mock.xack.assert_awaited_once_with("stream:jira", CONSUMER_GROUP, "1-0")

    @pytest.mark.asyncio
    async def test_terminal_callback_failure_is_retried_until_success(self):
        """An outbox entry remains after failure and is removed after later success."""
        callback = AsyncMock(side_effect=[RuntimeError("Jira unavailable"), None])
        consumer = make_consumer()
        consumer._terminal_failure_handler = callback
        message = make_message()
        notification = TerminalNotification(
            message=message,
            error="failed",
            serialized=b'{"event":"evt-001"}',
            lease_until=0,
        )
        consumer._retry_queue.claim_due_terminal_notifications = AsyncMock(
            side_effect=[[notification], [notification]]
        )

        await consumer._process_terminal_notifications_once()
        consumer._retry_queue.remove_terminal_notification.assert_not_awaited()

        await consumer._process_terminal_notifications_once()
        assert callback.await_count == 2
        consumer._retry_queue.remove_terminal_notification.assert_awaited_once_with(notification)


# ---------------------------------------------------------------------------
# _process_retry_queue — background poller
# ---------------------------------------------------------------------------


class TestProcessRetryQueue:
    @pytest.mark.asyncio
    async def test_successful_retry_removes_entry(self):
        """When retry dispatch succeeds, remove_from_retry is called."""
        consumer = make_consumer()
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=1,
            next_retry=datetime.utcnow(),
            last_error="boom",
        )

        call_count = 0

        async def claim_due_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [entry]
            consumer._running = False
            return []

        consumer._retry_queue.claim_due_messages = AsyncMock(side_effect=claim_due_side_effect)
        handler = AsyncMock()
        consumer.register_handler(EventSource.JIRA, handler)

        with patch("forge.queue.consumer.asyncio.sleep", new_callable=AsyncMock):
            await consumer._process_retry_queue()

        consumer._retry_queue.remove_from_retry.assert_awaited_once_with(entry)
        consumer._retry_queue.enqueue_for_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_retry_reenqueues(self):
        """When retry dispatch fails, the message is re-enqueued."""
        consumer = make_consumer()
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=1,
            next_retry=datetime.utcnow(),
            last_error="boom",
        )

        call_count = 0

        async def claim_due_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [entry]
            consumer._running = False
            return []

        consumer._retry_queue.claim_due_messages = AsyncMock(side_effect=claim_due_side_effect)
        failing_handler = AsyncMock(side_effect=RuntimeError("still broken"))
        consumer.register_handler(EventSource.JIRA, failing_handler)

        with patch("forge.queue.consumer.asyncio.sleep", new_callable=AsyncMock):
            await consumer._process_retry_queue()

        # On failure, the sorted-set entry is removed WITHOUT clearing the attempt
        # counter so the counter keeps accumulating toward the DLQ threshold.
        consumer._retry_queue.remove_from_retry_without_counter_reset.assert_awaited_once_with(
            entry
        )
        consumer._retry_queue.remove_from_retry.assert_not_called()
        consumer._retry_queue.enqueue_for_retry.assert_awaited_once()
        enqueue_args = consumer._retry_queue.enqueue_for_retry.call_args[0]
        assert enqueue_args[0].event_id == "evt-001"
        assert "still broken" in enqueue_args[1]

    @pytest.mark.asyncio
    async def test_exhausted_retry_invokes_terminal_callback(self):
        """The outbox poller delivers after the retry moves the event to the DLQ."""
        callback = AsyncMock()
        consumer = make_consumer()
        consumer._terminal_failure_handler = callback
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=3,
            next_retry=datetime.utcnow(),
            last_error="still broken",
        )
        notification = TerminalNotification(
            message=message,
            error="retries exhausted",
            serialized=b'{"event":"evt-001"}',
            lease_until=0,
        )
        consumer._retry_queue.claim_due_messages = AsyncMock(return_value=[entry])
        consumer._retry_queue.claim_due_terminal_notifications = AsyncMock(
            return_value=[notification]
        )
        consumer._retry_queue.enqueue_for_retry = AsyncMock(return_value=False)
        consumer.register_handler(
            EventSource.JIRA,
            AsyncMock(side_effect=RuntimeError("retries exhausted")),
        )

        await consumer._process_due_retries_once()
        await consumer._process_terminal_notifications_once()

        callback.assert_awaited_once_with(message, "retries exhausted")
        consumer._redis.xack.assert_awaited_once_with(
            "forge:events:jira", CONSUMER_GROUP, message.message_id
        )
        consumer._retry_queue.remove_terminal_notification.assert_awaited_once_with(notification)

    @pytest.mark.asyncio
    async def test_terminal_callback_failure_still_acknowledges_retry(self):
        """A callback failure cannot leave a terminal retry in the PEL."""
        callback = AsyncMock(side_effect=RuntimeError("Jira unavailable"))
        consumer = make_consumer()
        consumer._terminal_failure_handler = callback
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=3,
            next_retry=datetime.utcnow(),
            last_error="still broken",
        )

        notification = TerminalNotification(
            message=message,
            error="retries exhausted",
            serialized=b'{"event":"evt-001"}',
            lease_until=0,
        )
        consumer._retry_queue.claim_due_messages = AsyncMock(return_value=[entry])
        consumer._retry_queue.claim_due_terminal_notifications = AsyncMock(
            return_value=[notification]
        )
        consumer._retry_queue.enqueue_for_retry = AsyncMock(return_value=False)
        consumer.register_handler(
            EventSource.JIRA,
            AsyncMock(side_effect=RuntimeError("retries exhausted")),
        )

        await consumer._process_due_retries_once()
        await consumer._process_terminal_notifications_once()

        callback.assert_awaited_once_with(message, "retries exhausted")
        consumer._redis.xack.assert_awaited_once_with(
            "forge:events:jira", CONSUMER_GROUP, message.message_id
        )
        consumer._retry_queue.remove_terminal_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_retry_queue_no_action(self):
        """Poller with no due messages does nothing."""
        consumer = make_consumer()

        call_count = 0

        async def claim_due_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                consumer._running = False
            return []

        consumer._retry_queue.claim_due_messages = AsyncMock(side_effect=claim_due_side_effect)

        with patch("forge.queue.consumer.asyncio.sleep", new_callable=AsyncMock):
            await consumer._process_retry_queue()

        consumer._retry_queue.remove_from_retry.assert_not_called()
        consumer._retry_queue.enqueue_for_retry.assert_not_called()
