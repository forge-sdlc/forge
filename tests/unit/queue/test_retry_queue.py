"""Unit tests for RetryQueue class."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.queue.models import QueueMessage
from forge.queue.retry import (
    DEAD_LETTER_KEY,
    MAX_RETRY_ATTEMPTS,
    RETRY_ATTEMPTS_HASH_KEY,
    RETRY_CLAIM_LEASE_SECONDS,
    RETRY_QUEUE_KEY,
    RETRY_RECORDS_KEY,
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
    mock.eval = AsyncMock(return_value=[1, 1])
    mock.zrem = AsyncMock()
    mock.hset = AsyncMock()
    mock.hdel = AsyncMock()
    mock.hexists = AsyncMock(return_value=False)
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
        rq._redis = redis

        message = make_message()
        result = await rq.enqueue_for_retry(message, "timeout")

        assert result is True
        redis.eval.assert_awaited_once()
        args = redis.eval.await_args.args
        assert args[1] == 5
        assert args[2:7] == (
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            RETRY_ATTEMPTS_HASH_KEY,
            DEAD_LETTER_KEY,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
        )
        assert args[7] == f"jira:{message.message_id}"
        candidates = json.loads(args[8])
        assert candidates[0]["attempt"] == 1
        assert candidates[0]["message"]["message_id"] == message.message_id

    @pytest.mark.asyncio
    async def test_exceeds_max_attempts_moves_to_dlq(self):
        """When attempt count exceeds MAX_RETRY_ATTEMPTS, returns False and writes to DLQ."""
        rq = RetryQueue()
        redis = make_redis_mock()
        redis.eval = AsyncMock(return_value=[0, MAX_RETRY_ATTEMPTS + 1])
        rq._redis = redis

        message = make_message()
        result = await rq.enqueue_for_retry(message, "final failure")

        assert result is False
        eval_args = redis.eval.await_args.args
        assert eval_args[1] == 5
        assert eval_args[2:7] == (
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            RETRY_ATTEMPTS_HASH_KEY,
            DEAD_LETTER_KEY,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
        )

    @pytest.mark.asyncio
    async def test_backoff_increases_with_attempt(self):
        """Verify exponential backoff: second attempt has longer delay than first."""
        rq = RetryQueue()

        redis = make_redis_mock()

        message = make_message()

        rq._redis = redis
        await rq.enqueue_for_retry(message, "err")
        candidates = json.loads(redis.eval.await_args.args[8])
        scores = [candidate["next_retry_score"] for candidate in candidates]
        assert len(scores) == MAX_RETRY_ATTEMPTS + 1
        assert scores[1] > scores[0]
        assert scores[2] - scores[1] > scores[1] - scores[0]


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
        claimed_data = dict(raw_entry, claim_token="batch:1", lease_until=1_700_000_000)
        redis.eval = AsyncMock(return_value=[b"jira:1-0", json.dumps(claimed_data).encode()])
        rq._redis = redis

        results = await rq.claim_due_messages()

        assert len(results) == 1
        assert results[0].message.event_id == "evt-001"
        assert results[0].attempt == 1
        assert results[0].claim_token == "batch:1"
        assert results[0].lease_until == 1_700_000_000
        eval_args = redis.eval.await_args.args
        assert eval_args[1] == 3
        assert eval_args[2] == RETRY_QUEUE_KEY
        assert eval_args[3] == RETRY_RECORDS_KEY
        assert eval_args[4] == RETRY_ATTEMPTS_HASH_KEY
        assert eval_args[6] - eval_args[5] == RETRY_CLAIM_LEASE_SECONDS
        assert eval_args[7] == 10

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
        claim_lock = asyncio.Lock()
        entry_is_due = True

        async def atomic_eval(*_args):
            nonlocal entry_is_due
            async with claim_lock:
                if not entry_is_due:
                    return []
                entry_is_due = False
                claimed = dict(entry.to_dict(), claim_token="claim:1", lease_until=900)
                return [b"jira:1-0", json.dumps(claimed).encode()]

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
        score = 0.0

        async def eval_script(script, _numkeys, *_keys_and_args):
            nonlocal score
            if "ZRANGEBYSCORE" in script:
                now, lease_until, _limit, token = _keys_and_args[3:]
                if score <= now:
                    score = float(lease_until)
                    claimed = dict(
                        entry.to_dict(), claim_token=f"{token}:1", lease_until=lease_until
                    )
                    return [b"jira:1-0", json.dumps(claimed).encode()]
                return []
            member, _token, expected_score, lease_until, now = _keys_and_args[2:]
            if member == "jira:1-0" and score == float(expected_score) and now < expected_score:
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
            side_effect=[0, 600, 700, 901],
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
                "failed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
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

        redis.zrem.assert_awaited_once_with(RETRY_QUEUE_KEY, "jira:1-0")
        redis.hdel.assert_any_await(RETRY_RECORDS_KEY, "jira:1-0")
        redis.hdel.assert_any_await(RETRY_ATTEMPTS_HASH_KEY, "jira:1-0")

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

        redis.zrem.assert_awaited_once_with(RETRY_QUEUE_KEY, "jira:1-0")
        redis.hdel.assert_awaited_once_with(RETRY_RECORDS_KEY, "jira:1-0")
        assert redis.hdel.await_args.args[0] != RETRY_ATTEMPTS_HASH_KEY


class TestRequeueDeadLetter:
    @pytest.mark.asyncio
    async def test_removes_stale_terminal_notification(self):
        message = make_message()
        stored = json.dumps(
            {
                "message": {**message.to_dict(), "message_id": message.message_id},
                "error": "final failure",
                "attempts": 4,
                "failed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
        ).encode()
        redis = make_redis_mock()
        redis.lrange = AsyncMock(return_value=[stored])
        pipeline = MagicMock()
        pipeline.__aenter__ = AsyncMock(return_value=pipeline)
        pipeline.__aexit__ = AsyncMock(return_value=None)
        pipeline.hdel = MagicMock(return_value=pipeline)
        pipeline.hset = MagicMock(return_value=pipeline)
        pipeline.zadd = MagicMock(return_value=pipeline)
        pipeline.zrem = MagicMock(return_value=pipeline)
        pipeline.lset = MagicMock(return_value=pipeline)
        pipeline.lrem = MagicMock(return_value=pipeline)
        pipeline.execute = AsyncMock()
        redis.pipeline = MagicMock(return_value=pipeline)
        rq = RetryQueue()
        rq._redis = redis

        assert await rq.requeue_dead_letter(0)

        pipeline.zrem.assert_called_once_with(TERMINAL_NOTIFICATION_QUEUE_KEY, stored)
        pipeline.execute.assert_awaited_once()
