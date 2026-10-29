"""Crash recovery and retry ownership tests backed by real Redis."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

import forge.queue.consumer as consumer_module
from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage
from forge.queue.producer import JIRA_STREAM, QueueProducer
from forge.queue.retry import (
    RETRY_ATTEMPTS_HASH_KEY,
    RETRY_QUEUE_KEY,
    RETRY_RECORDS_KEY,
    RetryEntry,
    RetryQueue,
    _now_timestamp,
)


@pytest.mark.integration
class TestPendingDeliveryRecovery:
    async def test_abandoned_pending_message_is_reclaimed_and_acknowledged(self, redis_client):
        producer = QueueProducer(redis_client=redis_client)
        message_id = await producer.publish(
            event_id="recover-pending-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-RECOVER",
            payload={},
        )
        original_owner = QueueConsumer("crashed-worker", redis_client=redis_client)
        await original_owner._ensure_consumer_groups()
        raw = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            original_owner.consumer_name,
            {JIRA_STREAM: ">"},
            count=1,
        )
        message = QueueMessage.from_redis(*raw[0][1][0])
        assert message.message_id == message_id

        recovery = QueueConsumer(
            "recovery-worker",
            redis_client=redis_client,
            max_outstanding_deliveries=2,
            pending_idle_ms=1,
        )
        processed = asyncio.Event()

        async def handle(_message: QueueMessage) -> None:
            processed.set()

        recovery.register_handler(EventSource.JIRA, handle)
        recovery._running = True
        await asyncio.sleep(0.01)

        assert await recovery._recover_pending_once(JIRA_STREAM) == 1
        await asyncio.wait_for(processed.wait(), timeout=2)
        for _ in range(50):
            pending = await redis_client.xpending(JIRA_STREAM, CONSUMER_GROUP)
            if pending["pending"] == 0 and recovery._reserved_deliveries == 0:
                break
            await asyncio.sleep(0.01)
        assert pending["pending"] == 0
        assert recovery._reserved_deliveries == 0

    async def test_live_delivery_renewal_prevents_reclaim(self, redis_client, monkeypatch):
        producer = QueueProducer(redis_client=redis_client)
        await producer.publish(
            event_id="recover-live-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-LIVE",
            payload={},
        )
        original_owner = QueueConsumer("live-worker", redis_client=redis_client)
        await original_owner._ensure_consumer_groups()
        raw = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            original_owner.consumer_name,
            {JIRA_STREAM: ">"},
            count=1,
        )
        message = QueueMessage.from_redis(*raw[0][1][0])

        recovery = QueueConsumer(
            "recovery-worker",
            redis_client=redis_client,
            max_outstanding_deliveries=2,
            pending_idle_ms=30,
        )
        recovery._running = True
        monkeypatch.setattr(consumer_module, "STREAM_DELIVERY_RENEW_SECONDS", 0.005)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def keep_delivery_live():
            async with original_owner._renew_stream_delivery(message, JIRA_STREAM):
                entered.set()
                await release.wait()

        live_task = asyncio.create_task(keep_delivery_live())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.08)
        assert await recovery._recover_pending_once(JIRA_STREAM) == 0
        pending = await redis_client.xpending_range(
            JIRA_STREAM, CONSUMER_GROUP, message.message_id, message.message_id, 1
        )
        assert pending[0]["consumer"] == original_owner.consumer_name

        release.set()
        await live_task


@pytest.mark.integration
class TestRetryHandoff:
    async def test_retry_handoff_is_stable_and_claim_mutations_are_fenced(self, redis_client):
        producer = QueueProducer(redis_client=redis_client)
        message_id = await producer.publish(
            event_id="retry-handoff-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-RETRY",
            payload={},
        )
        consumer = QueueConsumer("retry-owner", redis_client=redis_client)
        await consumer._ensure_consumer_groups()
        raw = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=1,
        )
        message = QueueMessage.from_redis(*raw[0][1][0])
        assert message.message_id == message_id

        retries = RetryQueue()
        retries._redis = redis_client
        assert await retries.enqueue_for_retry(
            message,
            "temporary failure",
            stream=JIRA_STREAM,
            consumer_group=CONSUMER_GROUP,
        )
        assert await retries.enqueue_for_retry(
            message,
            "duplicate failure",
            stream=JIRA_STREAM,
            consumer_group=CONSUMER_GROUP,
        )
        identity = f"jira:{message_id}"
        assert await redis_client.hget(RETRY_RECORDS_KEY, identity)
        assert int(await redis_client.hget("forge:retry:attempt-counts", identity)) == 1
        assert (await redis_client.xpending(JIRA_STREAM, CONSUMER_GROUP))["pending"] == 0

        # Make the durable retry due without waiting for its backoff window.
        await redis_client.zadd(RETRY_QUEUE_KEY, {identity: _now_timestamp() - 1})
        claimed = (await retries.claim_due_messages(limit=1))[0]
        stale = type(claimed)(
            message=claimed.message,
            attempt=claimed.attempt,
            next_retry=claimed.next_retry,
            last_error=claimed.last_error,
            lease_until=claimed.lease_until,
            claim_token="stale-owner-token",
        )
        assert not await retries.reschedule_retry(
            stale,
            "stale failure",
            stream=JIRA_STREAM,
            consumer_group=CONSUMER_GROUP,
        )
        assert await retries.reschedule_retry(
            claimed,
            "second failure",
            stream=JIRA_STREAM,
            consumer_group=CONSUMER_GROUP,
        )
        stored = json.loads(await redis_client.hget(RETRY_RECORDS_KEY, identity))
        assert stored["attempt"] == 2

    async def test_legacy_duplicate_does_not_replace_active_canonical_claim(self, redis_client):
        message = QueueMessage(
            message_id="1720000000000-0",
            event_id="legacy-duplicate-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-LEGACY",
        )
        retries = RetryQueue()
        retries._redis = redis_client
        assert await retries.enqueue_for_retry(message, "initial failure")
        identity = f"jira:{message.message_id}"
        await redis_client.zadd(RETRY_QUEUE_KEY, {identity: _now_timestamp() - 1})
        active = (await retries.claim_due_messages(limit=1))[0]
        original_raw = await redis_client.hget(RETRY_RECORDS_KEY, identity)
        original_score = await redis_client.zscore(RETRY_QUEUE_KEY, identity)

        legacy = RetryEntry(
            message=message,
            attempt=3,
            next_retry=datetime.now(UTC).replace(tzinfo=None),
            last_error="old-format duplicate",
        )
        legacy_member = json.dumps(legacy.to_dict())
        await redis_client.zadd(RETRY_QUEUE_KEY, {legacy_member: _now_timestamp() - 1})

        assert await retries.claim_due_messages(limit=1) == []
        assert await redis_client.hget(RETRY_RECORDS_KEY, identity) == original_raw
        assert await redis_client.zscore(RETRY_QUEUE_KEY, identity) == original_score
        assert int(await redis_client.hget(RETRY_ATTEMPTS_HASH_KEY, identity)) == 1
        assert not await redis_client.zscore(RETRY_QUEUE_KEY, legacy_member)
        assert active.claim_token is not None
