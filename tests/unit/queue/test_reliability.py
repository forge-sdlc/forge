"""Focused regressions for queue ownership, capacity, and shutdown behavior."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import LockNotOwnedError

import forge.queue.consumer as consumer_module
from forge.models.events import EventSource
from forge.queue.consumer import (
    CONSUMER_GROUP,
    TICKET_LOCK_KEY_PREFIX,
    QueueConsumer,
    TicketLockLostError,
)
from forge.queue.metrics import QUEUE_ACTIVE_DELIVERIES, QUEUE_BUFFERED_DELIVERIES
from forge.queue.models import QueueMessage


class _Lock:
    def __init__(self, *, lost: bool = False) -> None:
        self.local = SimpleNamespace(token=b"test-owner")
        self.lost = lost

    async def acquire(self) -> bool:
        return True

    async def extend(self, _seconds: int, *, replace_ttl: bool) -> bool:
        assert replace_ttl
        if self.lost:
            raise LockNotOwnedError("ownership token no longer matches")
        return True

    async def release(self) -> None:
        return None


def _redis_mock(*, lost_lock: bool = False) -> MagicMock:
    redis = MagicMock()
    redis.lock = MagicMock(return_value=_Lock(lost=lost_lock))
    redis.xack = AsyncMock(return_value=1)
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.eval = AsyncMock(return_value=1)
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


def _message(ticket: str, message_id: str = "1-0") -> QueueMessage:
    return QueueMessage(
        message_id=message_id,
        event_id=f"event-{message_id}",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key=ticket,
    )


class TestQueueOwnership:
    @pytest.mark.asyncio
    async def test_prepare_resolves_ticket_before_distributed_lock(self):
        redis = _redis_mock()
        prepared = _message("CANONICAL-42")

        async def prepare(message: QueueMessage) -> QueueMessage:
            return replace(message, ticket_key=prepared.ticket_key)

        consumer = QueueConsumer(
            "worker",
            redis_client=redis,
            prepare_message=prepare,
        )
        handled = AsyncMock()
        consumer.register_handler(EventSource.JIRA, handled)

        await consumer._process_message(_message(""), "jira-events")

        redis.lock.assert_called_once()
        assert redis.lock.call_args.args[0] == f"{TICKET_LOCK_KEY_PREFIX}CANONICAL-42"
        assert handled.await_args.args[0].ticket_key == "CANONICAL-42"

    @pytest.mark.asyncio
    async def test_lock_not_owned_exception_cancels_owner(self, monkeypatch):
        monkeypatch.setattr(consumer_module, "TICKET_LOCK_RENEW_SECONDS", 0.001)
        consumer = QueueConsumer("worker", redis_client=_redis_mock(lost_lock=True))

        async def owned_work() -> None:
            async with consumer._distributed_ticket_lock("CANONICAL-42"):
                await asyncio.Event().wait()

        task = asyncio.create_task(owned_work())
        with pytest.raises(TicketLockLostError, match="Lost distributed ticket lock"):
            await asyncio.wait_for(task, timeout=1)


class TestQueueCapacity:
    @pytest.mark.asyncio
    async def test_stream_read_is_reserved_and_active_capacity_releases_once(self):
        redis = _redis_mock()
        consumer = QueueConsumer(
            "worker",
            redis_client=redis,
            max_outstanding_deliveries=3,
        )
        started = asyncio.Event()
        release = asyncio.Event()
        buffered_before = QUEUE_BUFFERED_DELIVERIES._value.get()
        active_before = QUEUE_ACTIVE_DELIVERIES._value.get()
        messages = [
            ("1-0", _message("TICKET-A", "1-0").to_dict()),
            ("2-0", _message("TICKET-B", "2-0").to_dict()),
        ]
        redis.xreadgroup = AsyncMock(return_value=[("jira-events", messages)])
        started_tickets: set[str] = set()

        async def handler(message: QueueMessage) -> None:
            started_tickets.add(message.ticket_key)
            if len(started_tickets) == 2:
                started.set()
            await release.wait()

        consumer.register_handler(EventSource.JIRA, handler)
        consumer._running = True
        consume = asyncio.create_task(consumer._consume_stream("jira-events", EventSource.JIRA))
        await asyncio.wait_for(started.wait(), timeout=1)

        assert redis.xreadgroup.await_count == 1
        assert redis.xreadgroup.call_args.kwargs["count"] == 2
        assert consumer._reserved_deliveries == 2

        consumer._running = False
        async with consumer._capacity_condition:
            consumer._capacity_condition.notify_all()
        release.set()
        await asyncio.wait_for(consume, timeout=1)
        await asyncio.sleep(0)

        assert consumer._reserved_deliveries == 0
        assert QUEUE_BUFFERED_DELIVERIES._value.get() == buffered_before
        assert QUEUE_ACTIVE_DELIVERIES._value.get() == active_before

    @pytest.mark.asyncio
    async def test_malformed_read_releases_reserved_capacity(self):
        redis = _redis_mock()
        consumer = QueueConsumer(
            "worker",
            redis_client=redis,
            max_outstanding_deliveries=2,
        )
        reads = 0

        async def read(*_args, **_kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                return [("jira-events", [("1-0", {"payload": "{"})])]
            consumer._running = False
            return []

        redis.xreadgroup = AsyncMock(side_effect=read)
        consumer.register_handler(EventSource.JIRA, AsyncMock())
        consumer._running = True
        await consumer._consume_stream("jira-events", EventSource.JIRA)

        assert reads == 2
        assert consumer._reserved_deliveries == 0
        redis.hset.assert_awaited_once()
        redis.xack.assert_awaited_once_with("jira-events", CONSUMER_GROUP, "1-0")

    @pytest.mark.asyncio
    async def test_shutdown_timeout_cancels_without_acknowledging_delivery(self):
        redis = _redis_mock()
        consumer = QueueConsumer(
            "worker",
            redis_client=redis,
            max_outstanding_deliveries=2,
            shutdown_timeout_seconds=0.01,
        )
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def handler(_message: QueueMessage) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        consumer.register_handler(EventSource.JIRA, handler)
        consumer._running = True
        assert await consumer._reserve_capacity(1, recovery=True) == 1
        task = consumer._dispatch_reserved(
            consumer_module._Delivery(_message("PENDING-1"), "jira-events")
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await consumer.stop()
        await asyncio.sleep(0)

        assert task.cancelled()
        assert stopped.is_set()
        redis.xack.assert_not_awaited()
        assert consumer._reserved_deliveries == 0
