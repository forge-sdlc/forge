"""Unit tests for atomic queue publication and deduplication."""

from unittest.mock import AsyncMock

from forge.models.events import EventSource
from forge.queue.deduplication import DEDUP_KEY_PREFIX, DEDUP_TTL_SECONDS
from forge.queue.producer import JIRA_STREAM, QueueProducer


async def test_publish_once_returns_stream_id_for_new_event() -> None:
    redis_client = AsyncMock()
    redis_client.eval.return_value = "123-0"
    producer = QueueProducer(redis_client=redis_client)

    message_id = await producer.publish_once(
        event_id="delivery-1",
        source=EventSource.JIRA,
        event_type="issue_created",
        ticket_key="TEST-1",
        payload={"issue": {"key": "TEST-1"}},
    )

    assert message_id == "123-0"
    args = redis_client.eval.await_args.args
    assert args[1:5] == (
        2,
        f"{DEDUP_KEY_PREFIX}delivery-1",
        JIRA_STREAM,
        DEDUP_TTL_SECONDS,
    )


async def test_publish_once_returns_none_for_duplicate_event() -> None:
    redis_client = AsyncMock()
    redis_client.eval.return_value = None
    producer = QueueProducer(redis_client=redis_client)

    message_id = await producer.publish_once(
        event_id="delivery-1",
        source=EventSource.JIRA,
        event_type="issue_created",
        ticket_key="TEST-1",
        payload={},
    )

    assert message_id is None
