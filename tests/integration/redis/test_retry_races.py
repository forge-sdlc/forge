"""Retry handoff races against shared Redis state."""

import asyncio
import json

import pytest

from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage
from forge.queue.producer import JIRA_STREAM, QueueProducer
from forge.queue.retry import (
    DEAD_LETTER_KEY,
    MAX_RETRY_ATTEMPTS,
    RETRY_ATTEMPTS_HASH_KEY,
    RETRY_QUEUE_KEY,
    RETRY_RECORDS_KEY,
    TERMINAL_NOTIFICATION_QUEUE_KEY,
    RetryQueue,
    _now_timestamp,
)


@pytest.mark.integration
class TestRetryRaces:
    async def test_two_retry_clients_claim_due_record_once(self, redis_client):
        message = QueueMessage(
            message_id="1721000000000-0",
            event_id="retry-claim-race",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-CLAIM-RACE",
        )
        seed = RetryQueue(redis_client=redis_client)
        assert await seed.enqueue_for_retry(message, "initial failure")
        identity = f"jira:{message.message_id}"
        await redis_client.zadd(RETRY_QUEUE_KEY, {identity: _now_timestamp() - 1})

        first = RetryQueue(redis_client=redis_client)
        second = RetryQueue(redis_client=redis_client)
        claims = await asyncio.gather(
            first.claim_due_messages(limit=1),
            second.claim_due_messages(limit=1),
        )

        assert sorted(len(claim) for claim in claims) == [0, 1]
        owner = next(claim[0] for claim in claims if claim)
        assert owner.claim_token
        assert owner.lease_until is not None
        stored = await redis_client.hget(RETRY_RECORDS_KEY, identity)
        assert owner.claim_token in stored
        assert int(await redis_client.hget(RETRY_ATTEMPTS_HASH_KEY, identity)) == 1

    async def test_pending_recovery_racing_retry_poller_reschedules_once(self, redis_client):
        producer = QueueProducer(redis_client=redis_client)
        message_id = await producer.publish(
            event_id="retry-recovery-race",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-RECOVERY-RACE",
            payload={},
        )
        original_owner = QueueConsumer("old-worker", redis_client=redis_client)
        await original_owner._ensure_consumer_groups()
        raw = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            original_owner.consumer_name,
            {JIRA_STREAM: ">"},
            count=1,
        )
        message = QueueMessage.from_redis(*raw[0][1][0])
        assert message.message_id == message_id

        # Leave a scheduled retry alongside the old pending delivery to model
        # a recovery scan racing a retry poller during an upgrade/restart.
        retries = RetryQueue(redis_client=redis_client)
        assert await retries.enqueue_for_retry(message, "first failure")
        identity = f"jira:{message.message_id}"
        raw_entry = await redis_client.hget(RETRY_RECORDS_KEY, identity)
        previous_score = float(json.loads(raw_entry)["next_retry_score"])
        await redis_client.zadd(RETRY_QUEUE_KEY, {identity: _now_timestamp() - 1})

        recovery = QueueConsumer(
            "recovery-worker",
            redis_client=redis_client,
            max_outstanding_deliveries=2,
            pending_idle_ms=1,
        )
        recovery._retry_queue = RetryQueue(redis_client=redis_client)
        recovery._running = True
        poller = QueueConsumer("retry-poller", redis_client=redis_client)
        poller._retry_queue = RetryQueue(redis_client=redis_client)
        calls = 0

        async def failing_handler(_message: QueueMessage) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("second attempt failed")

        recovery.register_handler(EventSource.JIRA, failing_handler)
        poller.register_handler(EventSource.JIRA, failing_handler)
        await asyncio.sleep(0.01)

        await asyncio.gather(
            recovery._recover_pending_once(JIRA_STREAM),
            poller._process_due_retries_once(),
        )

        assert calls == 1
        stored = json.loads(await redis_client.hget(RETRY_RECORDS_KEY, identity))
        assert stored["attempt"] == 2
        assert stored["next_retry_score"] > previous_score
        assert int(await redis_client.hget(RETRY_ATTEMPTS_HASH_KEY, identity)) == 2
        assert (await redis_client.xpending(JIRA_STREAM, CONSUMER_GROUP))["pending"] == 0

    async def test_exhausted_handoff_survives_restart_and_clears_pending(self, redis_client):
        producer = QueueProducer(redis_client=redis_client)
        message_id = await producer.publish(
            event_id="retry-exhausted-restart",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-EXHAUSTED",
            payload={"issue": {"key": "TEST-EXHAUSTED"}},
        )
        consumer = QueueConsumer("exhausting-worker", redis_client=redis_client)
        await consumer._ensure_consumer_groups()
        raw = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=1,
        )
        message = QueueMessage.from_redis(*raw[0][1][0])
        assert message.message_id == message_id

        identity = f"jira:{message.message_id}"
        await redis_client.hset(RETRY_ATTEMPTS_HASH_KEY, identity, MAX_RETRY_ATTEMPTS)
        retries = RetryQueue(redis_client=redis_client)
        assert not await retries.enqueue_for_retry(
            message,
            "permanent failure",
            stream=JIRA_STREAM,
            consumer_group=CONSUMER_GROUP,
        )

        assert (await redis_client.xpending(JIRA_STREAM, CONSUMER_GROUP))["pending"] == 0
        dead_letters = await redis_client.lrange(DEAD_LETTER_KEY, 0, -1)
        assert len(dead_letters) == 1
        assert (await redis_client.zcard(TERMINAL_NOTIFICATION_QUEUE_KEY)) == 1
        assert not await redis_client.hget(RETRY_RECORDS_KEY, identity)

        # A fresh consumer can claim the durable notification after restart.
        after_restart = QueueConsumer("restarted-worker", redis_client=redis_client)
        after_restart._retry_queue = RetryQueue(redis_client=redis_client)
        notified: list[str] = []

        async def terminal_handler(failed: QueueMessage, error: str) -> None:
            notified.append(f"{failed.event_id}:{error}")

        after_restart._terminal_failure_handler = terminal_handler
        await after_restart._process_terminal_notifications_once()

        assert notified == ["retry-exhausted-restart:permanent failure"]
        assert await redis_client.llen(DEAD_LETTER_KEY) == 1
        assert await redis_client.zcard(TERMINAL_NOTIFICATION_QUEUE_KEY) == 0
