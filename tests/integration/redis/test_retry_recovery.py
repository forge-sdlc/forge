"""Real-Redis integration tests for consumer retry and dead-letter recovery."""

from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage
from forge.queue.producer import JIRA_STREAM, QueueProducer
from forge.queue.retry import RETRY_QUEUE_KEY


async def _claim_one(redis_client, consumer: QueueConsumer) -> QueueMessage:
    await consumer._ensure_consumer_groups()
    entries = await redis_client.xreadgroup(
        CONSUMER_GROUP,
        consumer.consumer_name,
        {JIRA_STREAM: ">"},
        count=1,
        block=1000,
    )
    message_id, fields = entries[0][1][0]
    return QueueMessage.from_redis(message_id, fields)


async def _make_retries_due(redis_client) -> None:
    members = await redis_client.zrange(RETRY_QUEUE_KEY, 0, -1)
    if members:
        await redis_client.zadd(RETRY_QUEUE_KEY, dict.fromkeys(members, 0))


async def _pending_count(redis_client) -> int:
    pending = await redis_client.xpending(JIRA_STREAM, CONSUMER_GROUP)
    return int(pending["pending"])


async def test_failed_message_succeeds_on_retry_and_clears_pending_state(redis_client) -> None:
    producer = QueueProducer(redis_client=redis_client)
    consumer = QueueConsumer("retry-success", redis_client=redis_client)
    consumer._retry_queue._redis = redis_client
    calls = 0

    async def fail_once(_message: QueueMessage) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")

    consumer.register_handler(EventSource.JIRA, fail_once)
    await producer.publish(
        event_id="retry-success-1",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key="TEST-RETRY",
        payload={},
    )
    message = await _claim_one(redis_client, consumer)

    await consumer._process_message(message, JIRA_STREAM)
    # The retry record and original acknowledgement commit atomically.
    assert await _pending_count(redis_client) == 0
    assert (await consumer._retry_queue.get_queue_stats())["retry_queue_depth"] == 1

    await _make_retries_due(redis_client)
    await consumer._process_due_retries_once()

    assert calls == 2
    assert await _pending_count(redis_client) == 0
    assert await consumer._retry_queue.get_queue_stats() == {
        "retry_queue_depth": 0,
        "dead_letter_depth": 0,
    }


async def test_exhausted_message_moves_to_dlq_and_clears_pending_state(redis_client) -> None:
    producer = QueueProducer(redis_client=redis_client)
    consumer = QueueConsumer("retry-dlq", redis_client=redis_client)
    consumer._retry_queue._redis = redis_client

    async def always_fail(_message: QueueMessage) -> None:
        raise RuntimeError("persistent failure")

    consumer.register_handler(EventSource.JIRA, always_fail)
    await producer.publish(
        event_id="retry-dlq-1",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key="TEST-DLQ",
        payload={},
    )
    message = await _claim_one(redis_client, consumer)
    await consumer._process_message(message, JIRA_STREAM)

    for _ in range(3):
        await _make_retries_due(redis_client)
        await consumer._process_due_retries_once()

    stats = await consumer._retry_queue.get_queue_stats()
    dead_letters = await consumer._retry_queue.get_dead_letter_entries()
    assert stats == {"retry_queue_depth": 0, "dead_letter_depth": 1}
    assert dead_letters[0]["message"]["event_id"] == "retry-dlq-1"
    assert dead_letters[0]["attempts"] == 4
    assert await _pending_count(redis_client) == 0
