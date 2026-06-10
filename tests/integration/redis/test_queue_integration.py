"""Integration tests for Redis queue with real Redis container.

These tests verify that messages actually flow through the queue system
using a real Redis instance via testcontainers.
"""

import asyncio

import pytest

from forge.models.events import EventSource
from forge.queue.consumer import CONSUMER_GROUP, QueueConsumer
from forge.queue.models import QueueMessage
from forge.queue.producer import GITHUB_STREAM, JIRA_STREAM, QueueProducer


@pytest.mark.integration
class TestQueueProducer:
    """Test QueueProducer with real Redis."""

    async def test_publish_jira_event(self, redis_client):
        """Publish a Jira event to the queue."""
        producer = QueueProducer(redis_client=redis_client)

        message_id = await producer.publish(
            event_id="event-123",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-123",
            payload={"issue": {"key": "TEST-123"}},
        )

        # Verify message was published
        assert message_id is not None
        assert "-" in message_id  # Redis stream IDs have format timestamp-sequence

        # Verify message is in the stream
        stream_len = await redis_client.xlen(JIRA_STREAM)
        assert stream_len == 1

    async def test_publish_github_event(self, redis_client):
        """Publish a GitHub event to the queue."""
        producer = QueueProducer(redis_client=redis_client)

        await producer.publish(
            event_id="gh-event-456",
            source=EventSource.GITHUB,
            event_type="check_run:completed",
            ticket_key="TEST-456",
            payload={"check_run": {"conclusion": "success"}},
        )

        # Verify message was published to GitHub stream
        stream_len = await redis_client.xlen(GITHUB_STREAM)
        assert stream_len == 1

    async def test_publish_multiple_events(self, redis_client):
        """Publish multiple events to verify FIFO ordering."""
        producer = QueueProducer(redis_client=redis_client)

        # Publish 5 events
        message_ids = []
        for i in range(5):
            msg_id = await producer.publish(
                event_id=f"event-{i}",
                source=EventSource.JIRA,
                event_type="issue_updated",
                ticket_key="TEST-123",
                payload={"sequence": i},
            )
            message_ids.append(msg_id)

        # Verify all messages are in the stream
        stream_len = await redis_client.xlen(JIRA_STREAM)
        assert stream_len == 5

        # Verify order is preserved (message IDs should be increasing)
        for i in range(1, len(message_ids)):
            assert message_ids[i] > message_ids[i - 1]


@pytest.mark.integration
class TestQueueConsumer:
    """Test QueueConsumer with real Redis."""

    async def test_consume_single_message(self, redis_client):
        """Consumer should receive and process a single message."""
        producer = QueueProducer(redis_client=redis_client)
        consumer = QueueConsumer(
            consumer_name="test-consumer-1",
            redis_client=redis_client,
        )

        # Track processed messages
        processed_messages = []

        async def handler(message: QueueMessage):
            processed_messages.append(message)

        consumer.register_handler(EventSource.JIRA, handler)

        # Publish a message
        await producer.publish(
            event_id="consume-test-1",
            source=EventSource.JIRA,
            event_type="issue_created",
            ticket_key="TEST-789",
            payload={"issue": {"key": "TEST-789"}},
        )

        # Start consumer and let it process
        await consumer._ensure_consumer_groups()
        consumer._running = True

        # Read and process one batch
        messages = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=10,
            block=1000,
        )

        for _stream_name, entries in messages:
            for message_id, data in entries:
                message = QueueMessage.from_redis(message_id, data)
                await consumer._process_message(message, JIRA_STREAM)

        # Verify message was processed
        assert len(processed_messages) == 1
        assert processed_messages[0].event_id == "consume-test-1"
        assert processed_messages[0].ticket_key == "TEST-789"

    async def test_fifo_ordering_per_ticket(self, redis_client):
        """Messages for the same ticket should be processed in order."""
        producer = QueueProducer(redis_client=redis_client)
        consumer = QueueConsumer(
            consumer_name="test-consumer-2",
            redis_client=redis_client,
        )

        # Track processing order
        processing_order = []

        async def handler(message: QueueMessage):
            processing_order.append(message.payload["sequence"])
            await asyncio.sleep(0.01)  # Small delay to ensure order matters

        consumer.register_handler(EventSource.JIRA, handler)

        # Publish 5 messages for the same ticket
        for i in range(5):
            await producer.publish(
                event_id=f"fifo-test-{i}",
                source=EventSource.JIRA,
                event_type="issue_updated",
                ticket_key="TEST-FIFO",
                payload={"sequence": i},
            )

        # Ensure consumer groups exist
        await consumer._ensure_consumer_groups()
        consumer._running = True

        # Process all messages
        messages = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=10,
            block=1000,
        )

        for _stream_name, entries in messages:
            for message_id, data in entries:
                message = QueueMessage.from_redis(message_id, data)
                await consumer._process_message(message, JIRA_STREAM)

        # Verify FIFO order was maintained
        assert processing_order == [0, 1, 2, 3, 4]

    async def test_consumer_group_creation(self, redis_client):
        """Consumer should create consumer groups if they don't exist."""
        consumer = QueueConsumer(
            consumer_name="test-consumer-3",
            redis_client=redis_client,
        )

        # Ensure streams exist first by publishing
        producer = QueueProducer(redis_client=redis_client)
        await producer.publish(
            event_id="setup",
            source=EventSource.JIRA,
            event_type="setup",
            ticket_key="SETUP",
            payload={},
        )

        # This should create the consumer group
        await consumer._ensure_consumer_groups()

        # Verify group exists by trying to read (would fail if group doesn't exist)
        try:
            await redis_client.xreadgroup(
                CONSUMER_GROUP,
                consumer.consumer_name,
                {JIRA_STREAM: ">"},
                count=1,
                block=100,
            )
        except Exception as e:
            pytest.fail(f"Consumer group should exist: {e}")

    async def test_message_acknowledgment(self, redis_client):
        """Acknowledged messages should not be redelivered."""
        producer = QueueProducer(redis_client=redis_client)
        consumer = QueueConsumer(
            consumer_name="test-consumer-4",
            redis_client=redis_client,
        )

        async def handler(message: QueueMessage):
            pass

        consumer.register_handler(EventSource.JIRA, handler)

        # Publish a message
        await producer.publish(
            event_id="ack-test-1",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-ACK",
            payload={},
        )

        await consumer._ensure_consumer_groups()
        consumer._running = True

        # Read and acknowledge
        messages = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=10,
            block=1000,
        )

        for _stream_name, entries in messages:
            for message_id, data in entries:
                message = QueueMessage.from_redis(message_id, data)
                await consumer._process_message(message, JIRA_STREAM)

        # Try to read again - should get no new messages
        messages = await redis_client.xreadgroup(
            CONSUMER_GROUP,
            consumer.consumer_name,
            {JIRA_STREAM: ">"},
            count=10,
            block=100,
        )

        assert len(messages) == 0


@pytest.mark.integration
class TestQueueMessageSerialization:
    """Test message serialization through the queue."""

    async def test_message_roundtrip(self, redis_client):
        """Message data should survive roundtrip through Redis."""
        producer = QueueProducer(redis_client=redis_client)

        original_payload = {
            "issue": {
                "key": "TEST-123",
                "fields": {
                    "summary": "Test issue",
                    "description": "With special chars: $@!#%",
                    "labels": ["forge:managed", "backend"],
                },
            },
            "changelog": {"items": [{"field": "status", "toString": "In Progress"}]},
        }

        await producer.publish(
            event_id="roundtrip-test",
            source=EventSource.JIRA,
            event_type="issue_updated",
            ticket_key="TEST-123",
            payload=original_payload,
        )

        # Read from stream directly
        messages = await redis_client.xrange(JIRA_STREAM, "-", "+")
        assert len(messages) == 1

        message_id, data = messages[0]
        message = QueueMessage.from_redis(message_id, data)

        # Verify all fields survived
        assert message.event_id == "roundtrip-test"
        assert message.source == EventSource.JIRA
        assert message.event_type == "issue_updated"
        assert message.ticket_key == "TEST-123"
        assert message.payload == original_payload
        assert message.payload["issue"]["fields"]["description"] == "With special chars: $@!#%"

    async def test_empty_payload(self, redis_client):
        """Empty payload should be handled correctly."""
        producer = QueueProducer(redis_client=redis_client)

        await producer.publish(
            event_id="empty-test",
            source=EventSource.GITHUB,
            event_type="ping",
            ticket_key="",
            payload={},
        )

        messages = await redis_client.xrange(GITHUB_STREAM, "-", "+")
        message_id, data = messages[0]
        message = QueueMessage.from_redis(message_id, data)

        assert message.payload == {}
        assert message.ticket_key == ""
