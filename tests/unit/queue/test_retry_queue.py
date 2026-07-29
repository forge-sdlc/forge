"""Unit tests for RetryQueue class."""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from forge.models.events import EventSource
from forge.queue.models import QueueMessage
from forge.queue.retry import (
    DEAD_LETTER_KEY,
    MAX_RETRY_ATTEMPTS,
    RETRY_CLAIM_LEASE_SECONDS,
    RETRY_QUEUE_KEY,
    TERMINAL_NOTIFICATION_QUEUE_KEY,
    RetryEntry,
    RetryQueue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_message(event_id: str = "evt-001", ticket_key: str = "TEST-123") -> QueueMessage:
    return QueueMessage(
        message_id="1-0",
        event_id=event_id,
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key=ticket_key,
    )


def make_redis_mock() -> AsyncMock:
    """Return an AsyncMock that mimics an async Redis client."""
    mock = AsyncMock()
    mock.incr = AsyncMock(return_value=1)
    mock.expire = AsyncMock()
    mock.zadd = AsyncMock()
    mock.zrangebyscore = AsyncMock(return_value=[])
    mock.eval = AsyncMock(return_value=[])
    mock.zrem = AsyncMock()
    mock.delete = AsyncMock()
    mock.rpush = AsyncMock()
    mock.lrange = AsyncMock(return_value=[])
    return mock


# ---------------------------------------------------------------------------
# enqueue_for_retry
# ---------------------------------------------------------------------------


class TestEnqueueForRetry:
    @pytest.mark.asyncio
    async def test_first_attempt_queued(self):
        """First failure queues the message for retry (returns True)."""
        rq = RetryQueue()
        redis = make_redis_mock()
        redis.incr = AsyncMock(return_value=1)  # first attempt
        rq._redis = redis

        message = make_message()
        result = await rq.enqueue_for_retry(message, "timeout")

        assert result is True
        redis.zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_exceeds_max_attempts_moves_to_dlq(self):
        """When attempt count exceeds MAX_RETRY_ATTEMPTS, returns False and writes to DLQ."""
        rq = RetryQueue()
        redis = make_redis_mock()
        redis.incr = AsyncMock(return_value=MAX_RETRY_ATTEMPTS + 1)
        rq._redis = redis

        message = make_message()
        result = await rq.enqueue_for_retry(message, "final failure")

        assert result is False
        eval_args = redis.eval.await_args.args
        assert eval_args[1] == 2
        assert eval_args[2:4] == (DEAD_LETTER_KEY, TERMINAL_NOTIFICATION_QUEUE_KEY)
        assert json.loads(eval_args[4])["message"]["event_id"] == message.event_id

    @pytest.mark.asyncio
    async def test_backoff_increases_with_attempt(self):
        """Verify exponential backoff: second attempt has longer delay than first."""
        rq = RetryQueue()

        scores: list[float] = []

        async def capture_zadd(_key, mapping):
            score = list(mapping.values())[0]
            scores.append(score)

        redis = make_redis_mock()
        redis.zadd = capture_zadd

        message = make_message()

        # Attempt 1
        redis.incr = AsyncMock(return_value=1)
        rq._redis = redis
        await rq.enqueue_for_retry(message, "err")

        # Attempt 2
        redis.incr = AsyncMock(return_value=2)
        await rq.enqueue_for_retry(message, "err")

        assert len(scores) == 2
        assert scores[1] > scores[0], "Second retry should have a later timestamp"


# ---------------------------------------------------------------------------
# claim_due_messages
# ---------------------------------------------------------------------------


class TestClaimDueMessages:
    @pytest.mark.asyncio
    async def test_returns_parsed_entries(self):
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=1,
            next_retry=datetime(2024, 1, 1),
            last_error="oops",
        )
        # Use the canonical serialisation path so the round-trip is consistent.
        raw_entry = entry.to_dict()

        rq = RetryQueue()
        redis = make_redis_mock()
        redis.eval = AsyncMock(return_value=[json.dumps(raw_entry).encode()])
        rq._redis = redis

        results = await rq.claim_due_messages()

        assert len(results) == 1
        assert results[0].message.event_id == "evt-001"
        assert results[0].attempt == 1
        eval_args = redis.eval.await_args.args
        assert eval_args[1] == 1
        assert eval_args[2] == RETRY_QUEUE_KEY
        assert eval_args[4] - eval_args[3] == RETRY_CLAIM_LEASE_SECONDS
        assert eval_args[5] == 10

    @pytest.mark.asyncio
    async def test_empty_queue_returns_empty_list(self):
        rq = RetryQueue()
        redis = make_redis_mock()
        redis.eval = AsyncMock(return_value=[])
        rq._redis = redis

        results = await rq.claim_due_messages()
        assert results == []

    @pytest.mark.asyncio
    async def test_concurrent_workers_receive_entry_once(self):
        """Two workers sharing Redis cannot claim the same due entry."""
        entry = RetryEntry(
            message=make_message(),
            attempt=1,
            next_retry=datetime(2024, 1, 1),
            last_error="oops",
        )
        serialized_entry = json.dumps(entry.to_dict()).encode()
        claim_lock = asyncio.Lock()
        entry_is_due = True

        async def atomic_eval(*_args):
            nonlocal entry_is_due
            async with claim_lock:
                if not entry_is_due:
                    return []
                entry_is_due = False
                return [serialized_entry]

        redis = make_redis_mock()
        redis.eval = AsyncMock(side_effect=atomic_eval)
        first = RetryQueue()
        second = RetryQueue()
        first._redis = redis
        second._redis = redis

        claims = await asyncio.gather(
            first.claim_due_messages(),
            second.claim_due_messages(),
        )

        assert sorted(len(claim) for claim in claims) == [0, 1]
        assert redis.eval.await_count == 2

    @pytest.mark.asyncio
    async def test_renewal_keeps_entry_hidden_past_initial_lease(self):
        entry = RetryEntry(
            message=make_message(),
            attempt=1,
            next_retry=datetime(2024, 1, 1),
            last_error="oops",
        )
        serialized = json.dumps(entry.to_dict()).encode()
        score = 0.0

        async def eval_script(script, _keys, _key, *args):
            nonlocal score
            if "ZRANGEBYSCORE" in script:
                now, lease_until, _limit = args
                if score <= now:
                    score = float(lease_until)
                    return [serialized]
                return []
            member, expected_score, lease_until = args
            if member == serialized.decode() and score == float(expected_score):
                score = float(lease_until)
                return 1
            return 0

        redis = make_redis_mock()
        redis.eval = AsyncMock(side_effect=eval_script)
        first = RetryQueue()
        second = RetryQueue()
        first._redis = redis
        second._redis = redis

        with patch(
            "forge.queue.retry._now_timestamp",
            side_effect=[0, 600, 901],
        ):
            claimed = (await first.claim_due_messages())[0]
            initial_lease = claimed.lease_until

            assert await first.renew_retry_claim(claimed)
            assert claimed.lease_until is not None
            assert initial_lease is not None
            assert claimed.lease_until > initial_lease

            assert initial_lease == 900
            assert await second.claim_due_messages() == []


class TestTerminalNotificationQueue:
    @pytest.mark.asyncio
    async def test_concurrent_workers_claim_notification_once(self):
        message = make_message()
        stored = json.dumps(
            {
                "message": {**message.to_dict(), "message_id": message.message_id},
                "error": "final failure",
                "attempts": 4,
                "failed_at": datetime.utcnow().isoformat(),
            }
        ).encode()
        claim_lock = asyncio.Lock()
        entry_is_due = True

        async def atomic_eval(*_args):
            nonlocal entry_is_due
            async with claim_lock:
                if not entry_is_due:
                    return []
                entry_is_due = False
                return [stored]

        redis = make_redis_mock()
        redis.eval = AsyncMock(side_effect=atomic_eval)
        first = RetryQueue()
        second = RetryQueue()
        first._redis = redis
        second._redis = redis

        claims = await asyncio.gather(
            first.claim_due_terminal_notifications(),
            second.claim_due_terminal_notifications(),
        )

        assert sorted(len(claim) for claim in claims) == [0, 1]
        notification = next(claim[0] for claim in claims if claim)
        assert notification.message.event_id == message.event_id
        assert notification.error == "final failure"


# ---------------------------------------------------------------------------
# remove_from_retry
# ---------------------------------------------------------------------------


class TestRemoveFromRetry:
    @pytest.mark.asyncio
    async def test_removes_entry_and_clears_counter(self):
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=1,
            next_retry=datetime(2024, 1, 1),
            last_error="err",
        )

        rq = RetryQueue()
        redis = make_redis_mock()
        rq._redis = redis

        # patch to_dict to return something serialisable
        with patch.object(entry, "to_dict", return_value={"stub": True}):
            await rq.remove_from_retry(entry)

        redis.zrem.assert_called_once_with(RETRY_QUEUE_KEY, json.dumps({"stub": True}))
        redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_entry_without_clearing_counter(self):
        """remove_from_retry_without_counter_reset calls zrem but NOT delete."""
        message = make_message()
        entry = RetryEntry(
            message=message,
            attempt=1,
            next_retry=datetime(2024, 1, 1),
            last_error="err",
        )

        rq = RetryQueue()
        redis = make_redis_mock()
        rq._redis = redis

        with patch.object(entry, "to_dict", return_value={"stub": True}):
            await rq.remove_from_retry_without_counter_reset(entry)

        redis.zrem.assert_called_once_with(RETRY_QUEUE_KEY, json.dumps({"stub": True}))
        redis.delete.assert_not_called()


class TestRequeueDeadLetter:
    @pytest.mark.asyncio
    async def test_removes_stale_terminal_notification(self):
        message = make_message()
        stored = json.dumps(
            {
                "message": {**message.to_dict(), "message_id": message.message_id},
                "error": "final failure",
                "attempts": 4,
                "failed_at": datetime.utcnow().isoformat(),
            }
        ).encode()
        redis = make_redis_mock()
        redis.lrange = AsyncMock(return_value=[stored])
        rq = RetryQueue()
        rq._redis = redis

        assert await rq.requeue_dead_letter(0)

        redis.zrem.assert_awaited_once_with(TERMINAL_NOTIFICATION_QUEUE_KEY, stored)
