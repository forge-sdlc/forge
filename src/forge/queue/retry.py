"""Webhook retry queue with dead-letter handling."""

import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from forge.orchestrator.checkpointer import get_redis_client
from forge.queue.models import QueueMessage
from forge.utils.time import utc_now_naive

logger = logging.getLogger(__name__)

# Redis key prefixes
RETRY_QUEUE_KEY = "forge:retry:queue"
RETRY_RECORDS_KEY = "forge:retry:records"
RETRY_ATTEMPTS_HASH_KEY = "forge:retry:attempt-counts"
DEAD_LETTER_KEY = "forge:retry:dlq"
RETRY_ATTEMPTS_KEY = "forge:retry:attempts"
TERMINAL_NOTIFICATION_QUEUE_KEY = "forge:retry:terminal-notifications"

# Retry configuration
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_MULTIPLIER = 2
INITIAL_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 3600  # 1 hour
RETRY_CLAIM_LEASE_SECONDS = 900  # 15 minutes
RETRY_CLAIM_RENEW_SECONDS = RETRY_CLAIM_LEASE_SECONDS // 3
TERMINAL_NOTIFICATION_LEASE_SECONDS = 300

# Atomically move due entries beyond the visibility window before returning
# them. Other pollers cannot claim the same entries while this lease is active;
# if a worker exits before cleanup, the entries become visible again.
_CLAIM_DUE_MESSAGES_SCRIPT = """
local members = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[1], "WITHSCORES", "LIMIT", 0, ARGV[3])
local results = {}
local sequence = 0
for i = 1, #members, 2 do
    local member = members[i]
    local score = tonumber(members[i + 1])
    local identity = member
    local raw = redis.call("HGET", KEYS[2], identity)
    if not raw and string.sub(member, 1, 1) == "{" then
        local ok, legacy = pcall(cjson.decode, member)
        if ok and legacy.message then
            local source = legacy.message.source or "legacy"
            local message_id = legacy.message.message_id
            if not message_id or message_id == "" then
                message_id = "legacy:" .. tostring(legacy.message.event_id or "unknown")
            end
            identity = source .. ":" .. message_id
            raw = redis.call("HGET", KEYS[2], identity)
            redis.call("ZREM", KEYS[1], member)
            if not raw then
                legacy.claim_token = nil
                legacy.lease_until = nil
                raw = cjson.encode(legacy)
                redis.call("HSET", KEYS[2], identity, raw)
                redis.call("ZADD", KEYS[1], score, identity)
                redis.call("HSET", KEYS[3], identity, tonumber(legacy.attempt or 1))
            end
        end
    end
    local identity_score = redis.call("ZSCORE", KEYS[1], identity)
    if raw and identity_score and tonumber(identity_score) <= tonumber(ARGV[1]) then
        sequence = sequence + 1
        local entry = cjson.decode(raw)
        entry.claim_token = ARGV[4] .. ":" .. tostring(sequence)
        entry.lease_until = tonumber(ARGV[2])
        raw = cjson.encode(entry)
        redis.call("HSET", KEYS[2], identity, raw)
        redis.call("ZADD", KEYS[1], ARGV[2], identity)
        table.insert(results, identity)
        table.insert(results, raw)
    end
end
return results
"""

_RENEW_CLAIM_SCRIPT = """
local raw = redis.call("HGET", KEYS[2], ARGV[1])
if not raw then return 0 end
local entry = cjson.decode(raw)
local score = redis.call("ZSCORE", KEYS[1], ARGV[1])
if entry.claim_token == ARGV[2] and score and tonumber(score) == tonumber(ARGV[3]) and tonumber(ARGV[5]) < tonumber(ARGV[3]) then
    entry.lease_until = tonumber(ARGV[4])
    redis.call("HSET", KEYS[2], ARGV[1], cjson.encode(entry))
    redis.call("ZADD", KEYS[1], ARGV[4], ARGV[1])
    return 1
end
return 0
"""

_MOVE_TO_DEAD_LETTER_SCRIPT = """
redis.call("RPUSH", KEYS[1], ARGV[1])
redis.call("ZADD", KEYS[2], ARGV[2], ARGV[1])
return 1
"""

_ENQUEUE_RETRY_SCRIPT = """
if redis.call("HEXISTS", KEYS[2], ARGV[1]) == 1 then
    if ARGV[7] ~= "" then redis.call("XACK", ARGV[7], ARGV[8], ARGV[9]) end
    return {1, 0}
end
local attempt = redis.call("HINCRBY", KEYS[3], ARGV[1], 1)
local candidates = cjson.decode(ARGV[2])
local entry = candidates[math.min(attempt, #candidates)]
local serialized = cjson.encode(entry)
if attempt > tonumber(ARGV[4]) then
    local dead = cjson.encode({message=entry.message, error=ARGV[3], attempts=attempt, failed_at=ARGV[5]})
    redis.call("RPUSH", KEYS[4], dead)
    redis.call("ZADD", KEYS[5], ARGV[6], dead)
    if ARGV[7] ~= "" then redis.call("XACK", ARGV[7], ARGV[8], ARGV[9]) end
    return {0, attempt}
end
redis.call("HSET", KEYS[2], ARGV[1], serialized)
redis.call("ZADD", KEYS[1], entry.next_retry_score, ARGV[1])
if ARGV[7] ~= "" then redis.call("XACK", ARGV[7], ARGV[8], ARGV[9]) end
return {1, attempt}
"""

_RESCHEDULE_RETRY_SCRIPT = """
local raw = redis.call("HGET", KEYS[2], ARGV[1])
if not raw then return -1 end
local entry = cjson.decode(raw)
if entry.claim_token ~= ARGV[2] or tonumber(entry.lease_until or -1) ~= tonumber(ARGV[3]) or tonumber(ARGV[8]) >= tonumber(ARGV[3]) then
    return -1
end
local attempt = redis.call("HINCRBY", KEYS[3], ARGV[1], 1)
local candidates = cjson.decode(ARGV[5])
local replacement = candidates[math.min(attempt, #candidates)]
if attempt > tonumber(ARGV[6]) then
    local dead = cjson.encode({message=entry.message, error=ARGV[4], attempts=attempt, failed_at=ARGV[7]})
    redis.call("RPUSH", KEYS[4], dead)
    redis.call("ZADD", KEYS[5], ARGV[8], dead)
    redis.call("ZREM", KEYS[1], ARGV[1])
    redis.call("HDEL", KEYS[2], ARGV[1])
    if ARGV[9] ~= "" then redis.call("XACK", ARGV[9], ARGV[10], ARGV[11]) end
    return 2
end
local serialized = cjson.encode(replacement)
redis.call("HSET", KEYS[2], ARGV[1], serialized)
redis.call("ZADD", KEYS[1], replacement.next_retry_score, ARGV[1])
if ARGV[9] ~= "" then redis.call("XACK", ARGV[9], ARGV[10], ARGV[11]) end
return 1
"""

_COMPLETE_RETRY_SCRIPT = """
local raw = redis.call("HGET", KEYS[2], ARGV[1])
if not raw then return 0 end
local entry = cjson.decode(raw)
if entry.claim_token ~= ARGV[2] or tonumber(entry.lease_until or -1) ~= tonumber(ARGV[3]) or tonumber(ARGV[6]) >= tonumber(ARGV[3]) then
    return 0
end
redis.call("XACK", KEYS[4], ARGV[4], ARGV[5])
redis.call("ZREM", KEYS[1], ARGV[1])
redis.call("HDEL", KEYS[2], ARGV[1])
redis.call("HDEL", KEYS[3], ARGV[1])
return 1
"""

_CLAIM_TERMINAL_NOTIFICATIONS_SCRIPT = """
local entries = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[1], "LIMIT", 0, ARGV[3])
for _, entry in ipairs(entries) do redis.call("ZADD", KEYS[1], ARGV[2], entry) end
return entries
"""


def _now_timestamp() -> float:
    return utc_now_naive().timestamp()


@dataclass
class RetryEntry:
    """An entry in the retry queue."""

    message: QueueMessage
    attempt: int
    next_retry: datetime
    last_error: str
    lease_until: float | None = None
    claim_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for Redis storage."""
        msg = self.message.to_dict()
        # Preserve message_id separately so round-trip is lossless.
        msg["message_id"] = self.message.message_id
        data = {
            "message": msg,
            "attempt": self.attempt,
            "next_retry": self.next_retry.isoformat(),
            "last_error": self.last_error,
        }
        if self.lease_until is not None:
            data["lease_until"] = self.lease_until
        if self.claim_token is not None:
            data["claim_token"] = self.claim_token
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetryEntry":
        """Create from dictionary loaded from Redis."""
        msg_data = dict(data["message"])  # shallow copy — do not mutate the caller's dict
        message_id = msg_data.pop("message_id", "")
        return cls(
            message=QueueMessage.from_redis(message_id, msg_data),
            attempt=data["attempt"],
            next_retry=datetime.fromisoformat(data["next_retry"]),
            last_error=data["last_error"],
            lease_until=data.get("lease_until"),
            claim_token=data.get("claim_token"),
        )


@dataclass
class TerminalNotification:
    """A leased terminal-failure notification from the durable outbox."""

    message: QueueMessage
    error: str
    serialized: str | bytes
    lease_until: float


class RetryQueue:
    """Manages webhook retry queue with exponential backoff and dead-letter."""

    def __init__(self, redis_client: Any = None) -> None:
        """Initialize retry queue, optionally sharing the consumer's Redis client."""
        self._redis: Any = redis_client

    async def _get_redis(self) -> Any:
        """Get Redis client."""
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    @staticmethod
    def _identity(message: QueueMessage) -> str:
        """Stable retry address that is independent of canonical ticket resolution."""
        return f"{message.source.value}:{message.message_id}"

    @staticmethod
    def _legacy_identity(message: QueueMessage) -> str:
        return f"{message.source.value}:{message.ticket_key}:{message.event_id}"

    @staticmethod
    def _candidate_payloads(message: QueueMessage, error: str) -> str:
        now = utc_now_naive()
        candidates = []
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 2):
            delay = min(
                INITIAL_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_MULTIPLIER ** (attempt - 1)),
                MAX_RETRY_DELAY_SECONDS,
            )
            next_retry = now + timedelta(seconds=delay)
            entry = RetryEntry(
                message=message,
                attempt=attempt,
                next_retry=next_retry,
                last_error=error,
            ).to_dict()
            entry["next_retry_score"] = next_retry.timestamp()
            candidates.append(entry)
        return json.dumps(candidates)

    async def enqueue_for_retry(
        self,
        message: QueueMessage,
        error: str,
        *,
        stream: str | None = None,
        consumer_group: str | None = None,
    ) -> bool:
        """Idempotently transfer a failed delivery to its stable retry record.

        Args:
            message: The failed message.
            error: Error message from the failure.

        Returns:
            True if queued for retry, False if moved to dead-letter.
        """
        redis = await self._get_redis()
        identity = self._identity(message)
        result = await redis.eval(
            _ENQUEUE_RETRY_SCRIPT,
            5,
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            RETRY_ATTEMPTS_HASH_KEY,
            DEAD_LETTER_KEY,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
            identity,
            self._candidate_payloads(message, error),
            error,
            MAX_RETRY_ATTEMPTS,
            utc_now_naive().isoformat(),
            _now_timestamp(),
            stream or "",
            consumer_group or "",
            message.message_id,
        )
        queued = bool(int(result[0]))
        if queued:
            logger.info("Transferred %s to durable retry record", identity)
        else:
            logger.warning("Message %s moved to dead-letter queue", identity)
        return queued

    async def reschedule_retry(
        self,
        entry: RetryEntry,
        error: str,
        *,
        stream: str,
        consumer_group: str,
    ) -> bool:
        """Reschedule a failed attempt only if its retry claim token is current."""
        if entry.claim_token is None or entry.lease_until is None:
            return False
        redis = await self._get_redis()
        identity = self._identity(entry.message)
        result = await redis.eval(
            _RESCHEDULE_RETRY_SCRIPT,
            5,
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            RETRY_ATTEMPTS_HASH_KEY,
            DEAD_LETTER_KEY,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
            identity,
            entry.claim_token,
            entry.lease_until,
            error,
            self._candidate_payloads(entry.message, error),
            MAX_RETRY_ATTEMPTS,
            utc_now_naive().isoformat(),
            _now_timestamp(),
            stream,
            consumer_group,
            entry.message.message_id,
            _now_timestamp(),
        )
        outcome = int(result)
        if outcome < 0:
            logger.warning("Rejected stale retry reschedule for %s", identity)
            return False
        return outcome == 1

    async def complete_retry(
        self,
        entry: RetryEntry,
        *,
        stream: str,
        consumer_group: str,
    ) -> bool:
        """Atomically validate retry ownership, remove the record, and XACK."""
        if entry.claim_token is None or entry.lease_until is None:
            return False
        redis = await self._get_redis()
        return (
            int(
                await redis.eval(
                    _COMPLETE_RETRY_SCRIPT,
                    4,
                    RETRY_QUEUE_KEY,
                    RETRY_RECORDS_KEY,
                    RETRY_ATTEMPTS_HASH_KEY,
                    stream,
                    self._identity(entry.message),
                    entry.claim_token,
                    entry.lease_until,
                    consumer_group,
                    entry.message.message_id,
                    _now_timestamp(),
                )
            )
            == 1
        )

    async def has_retry(self, message: QueueMessage) -> bool:
        """Return whether a durable retry record owns this stream delivery.

        Old releases stored serialized retry entries as sorted-set members. Read
        those records and migrate the matching entry without changing its score.
        """
        redis = await self._get_redis()
        identity = self._identity(message)
        if await redis.hexists(RETRY_RECORDS_KEY, identity):
            return True
        members = await redis.zrange(RETRY_QUEUE_KEY, 0, -1, withscores=True)
        for raw_member, score in members:
            try:
                decoded_member = (
                    raw_member.decode() if isinstance(raw_member, bytes) else raw_member
                )
                if not decoded_member.startswith("{"):
                    continue
                old = RetryEntry.from_dict(json.loads(decoded_member))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if old.message.message_id != message.message_id or old.message.source != message.source:
                continue
            serialized = json.dumps(old.to_dict())
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(RETRY_RECORDS_KEY, identity, serialized)
                pipe.hset(RETRY_ATTEMPTS_HASH_KEY, identity, old.attempt)
                pipe.zrem(RETRY_QUEUE_KEY, raw_member)
                pipe.zadd(RETRY_QUEUE_KEY, {identity: score})
                await pipe.execute()
            return True
        return False

    async def _move_to_dead_letter(
        self,
        message: QueueMessage,
        error: str,
        attempt: int,
    ) -> None:
        """Move a message to the dead-letter queue.

        Args:
            message: The failed message.
            error: Final error message.
            attempt: Number of attempts made.
        """
        redis = await self._get_redis()
        message_id = f"{message.source}:{message.ticket_key}:{message.event_id}"

        msg = message.to_dict()
        # Preserve message_id so round-trip through requeue_dead_letter is lossless.
        msg["message_id"] = message.message_id
        entry = {
            "message": msg,
            "error": error,
            "attempts": attempt,
            "failed_at": utc_now_naive().isoformat(),
        }

        serialized = json.dumps(entry)
        await redis.eval(
            _MOVE_TO_DEAD_LETTER_SCRIPT,
            2,
            DEAD_LETTER_KEY,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
            serialized,
            _now_timestamp(),
        )
        logger.warning(
            f"Message {message_id} moved to dead-letter queue after "
            f"{attempt} attempts. Error: {error}"
        )

    async def claim_due_messages(self, limit: int = 10) -> list[RetryEntry]:
        """Atomically lease and return messages that are due for retry.

        Args:
            limit: Maximum number of messages to return.

        Returns:
            List of retry entries exclusively leased to this poller for the
            visibility window.
        """
        redis = await self._get_redis()
        now = _now_timestamp()
        lease_until = now + RETRY_CLAIM_LEASE_SECONDS

        claimed = await redis.eval(
            _CLAIM_DUE_MESSAGES_SCRIPT,
            3,
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            RETRY_ATTEMPTS_HASH_KEY,
            now,
            lease_until,
            limit,
            uuid.uuid4().hex,
        )

        results = []
        if claimed and isinstance(claimed[0], (list, tuple)):
            pairs = claimed
        elif claimed and isinstance(claimed[0], (bytes, str)) and len(claimed) % 2 == 0:
            pairs = list(zip(claimed[::2], claimed[1::2], strict=True))
        else:
            pairs = []
        for _identity, entry_json in pairs:
            try:
                if isinstance(entry_json, bytes):
                    entry_json = entry_json.decode()
                data = json.loads(entry_json)
                entry = RetryEntry.from_dict(data)
                results.append(entry)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.error(f"Failed to parse retry entry: {e}")

        return results

    async def renew_retry_claim(self, entry: RetryEntry) -> bool:
        """Extend a retry lease only while this worker still owns it."""
        if entry.lease_until is None:
            return False

        redis = await self._get_redis()
        lease_until = _now_timestamp() + RETRY_CLAIM_LEASE_SECONDS
        renewed = await redis.eval(
            _RENEW_CLAIM_SCRIPT,
            2,
            RETRY_QUEUE_KEY,
            RETRY_RECORDS_KEY,
            self._identity(entry.message),
            entry.claim_token or "",
            entry.lease_until,
            lease_until,
            _now_timestamp(),
        )
        if renewed:
            entry.lease_until = lease_until
        return int(renewed) == 1

    async def claim_due_terminal_notifications(self, limit: int = 10) -> list[TerminalNotification]:
        """Lease due terminal notifications for exclusive delivery."""
        redis = await self._get_redis()
        now = _now_timestamp()
        lease_until = now + TERMINAL_NOTIFICATION_LEASE_SECONDS
        entries = await redis.eval(
            _CLAIM_TERMINAL_NOTIFICATIONS_SCRIPT,
            1,
            TERMINAL_NOTIFICATION_QUEUE_KEY,
            now,
            lease_until,
            limit,
        )

        results = []
        for serialized in entries:
            try:
                data = json.loads(serialized)
                msg_data = dict(data["message"])
                message_id = msg_data.pop("message_id", "")
                results.append(
                    TerminalNotification(
                        message=QueueMessage.from_redis(message_id, msg_data),
                        error=data["error"],
                        serialized=serialized,
                        lease_until=lease_until,
                    )
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error(f"Failed to parse terminal notification: {exc}")
        return results

    async def remove_terminal_notification(self, entry: TerminalNotification) -> None:
        """Remove an outbox entry after successful delivery."""
        redis = await self._get_redis()
        await redis.zrem(TERMINAL_NOTIFICATION_QUEUE_KEY, entry.serialized)

    async def remove_from_retry(self, entry: RetryEntry) -> None:
        """Remove a message from the retry queue after successful processing.

        Removes the sorted-set entry **and** clears the attempt counter so the
        message starts fresh if it ever fails again.

        Args:
            entry: The retry entry to remove.
        """
        redis = await self._get_redis()
        identity = self._identity(entry.message)
        await redis.zrem(RETRY_QUEUE_KEY, identity)
        await redis.hdel(RETRY_RECORDS_KEY, identity)
        await redis.hdel(RETRY_ATTEMPTS_HASH_KEY, identity)

    async def remove_from_retry_without_counter_reset(self, entry: RetryEntry) -> None:
        """Remove a message's sorted-set entry without clearing the attempt counter.

        Use this in the failure path so that the attempt counter keeps
        accumulating across re-enqueues.  If the counter were deleted here,
        ``enqueue_for_retry`` would call ``INCR`` on a missing key and Redis
        would reinitialise it to 1, preventing the message from ever reaching
        the dead-letter queue.

        Args:
            entry: The retry entry to remove from the sorted set.
        """
        redis = await self._get_redis()
        identity = self._identity(entry.message)
        await redis.zrem(RETRY_QUEUE_KEY, identity)
        await redis.hdel(RETRY_RECORDS_KEY, identity)

    async def get_dead_letter_entries(
        self,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get entries from the dead-letter queue for inspection.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of dead-letter entries.
        """
        redis = await self._get_redis()
        entries = await redis.lrange(DEAD_LETTER_KEY, 0, limit - 1)

        results = []
        for entry_json in entries:
            with contextlib.suppress(json.JSONDecodeError):
                results.append(json.loads(entry_json))

        return results

    async def requeue_dead_letter(self, index: int) -> bool:
        """Requeue a dead-letter entry for retry.

        Args:
            index: Index of the entry in the dead-letter queue.

        Returns:
            True if successfully requeued.
        """
        redis = await self._get_redis()
        entries = await redis.lrange(DEAD_LETTER_KEY, index, index)

        if not entries:
            return False

        try:
            data = json.loads(entries[0])
            msg_data = dict(data["message"])  # shallow copy — do not mutate the caller's dict
            stream_entry_id = msg_data.pop("message_id", "")
            message = QueueMessage.from_redis(stream_entry_id, msg_data)

            # Reset the stable per-stream attempt counter and create a new
            # addressable retry record before removing the DLQ handoff.
            identity = self._identity(message)
            entry = RetryEntry(
                message=message,
                attempt=1,
                next_retry=utc_now_naive() + timedelta(seconds=INITIAL_RETRY_DELAY_SECONDS),
                last_error="Requeued from dead-letter",
            )
            serialized = entry.to_dict()
            serialized["next_retry_score"] = entry.next_retry.timestamp()
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hdel(RETRY_ATTEMPTS_HASH_KEY, identity)
                pipe.hset(RETRY_RECORDS_KEY, identity, json.dumps(serialized))
                pipe.zadd(RETRY_QUEUE_KEY, {identity: entry.next_retry.timestamp()})
                pipe.zrem(TERMINAL_NOTIFICATION_QUEUE_KEY, entries[0])
                pipe.lset(DEAD_LETTER_KEY, index, "__REMOVED__")
                pipe.lrem(DEAD_LETTER_KEY, 1, "__REMOVED__")
                await pipe.execute()

            logger.info("Requeued %s from dead-letter queue", identity)
            return True

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to requeue dead-letter entry: {e}")
            return False

    async def get_queue_stats(self) -> dict[str, int]:
        """Get statistics about the retry and dead-letter queues.

        Returns:
            Dictionary with queue statistics.
        """
        redis = await self._get_redis()
        return {
            "retry_queue_depth": await redis.zcard(RETRY_QUEUE_KEY),
            "dead_letter_depth": await redis.llen(DEAD_LETTER_KEY),
        }
