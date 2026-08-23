"""Unit tests for atomic queue publication and deduplication."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from forge.integrations.source_control.contracts import (
    Actor,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
)
from forge.models.events import EventSource
from forge.queue.deduplication import DEDUP_KEY_PREFIX, DEDUP_TTL_SECONDS
from forge.queue.producer import JIRA_STREAM, SOURCE_CONTROL_STREAM, QueueProducer


def _sample_event() -> NormalizedEvent:
    repo_ref = RepositoryRef(
        id="acme/payments",
        provider=Provider.GITHUB,
        connection="default-github",
        namespace="acme/payments",
        default_branch="main",
        change_request_mode="fork",
    )
    return NormalizedEvent(
        id="delivery-1",
        kind=EventKind.CR_OPENED,
        repo_ref=repo_ref,
        actor=Actor(login="octocat", is_bot=False),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"action": "opened"},
    )


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


async def test_publish_event_returns_stream_id_for_new_event() -> None:
    """publish_event must use the same atomic dedup as publish_once -- GitHub
    redelivers webhooks on timeouts/5xx/manual redeliver, and a plain XADD
    would silently reprocess every retried delivery."""
    redis_client = AsyncMock()
    redis_client.eval.return_value = "456-0"
    producer = QueueProducer(redis_client=redis_client)

    message_id = await producer.publish_event(_sample_event(), ticket_key="PROJ-1")

    assert message_id == "456-0"
    args = redis_client.eval.await_args.args
    assert args[1:5] == (
        2,
        f"{DEDUP_KEY_PREFIX}delivery-1",
        SOURCE_CONTROL_STREAM,
        DEDUP_TTL_SECONDS,
    )


async def test_publish_event_returns_none_for_duplicate_event() -> None:
    redis_client = AsyncMock()
    redis_client.eval.return_value = None
    producer = QueueProducer(redis_client=redis_client)

    message_id = await producer.publish_event(_sample_event(), ticket_key="PROJ-1")

    assert message_id is None
