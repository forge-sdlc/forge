"""Durable execution timeline records and storage adapters.

The workflow checkpoint is the source of truth for control state, but it is not
an event log.  This module provides a small append-only boundary for the
operator timeline.  Both adapters use the same idempotency and ordering rules,
which makes rebuilding a projection from a checkpoint and its records
deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from forge.domain import JsonValue
from forge.orchestrator.checkpointer import get_redis_client
from forge.read_models.models import TimelineEntry

_PREFIX = "forge:execution-timeline:"
_EVENT_PREFIX = f"{_PREFIX}event:"

_APPEND_SCRIPT = """
-- The marker and list append must share one Redis atomic execution.  If a
-- client disappears after SETNX, a later retry must still be able to observe
-- that the complete operation committed (or retry the complete operation if
-- the script did not commit).
if redis.call('SETNX', KEYS[1], ARGV[1]) == 1 then
  redis.call('RPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""


def _sort_key(entry: TimelineEntry) -> tuple[datetime, str, str]:
    occurred = entry.occurred_at
    if occurred is None:
        occurred = datetime.min.replace(tzinfo=UTC)
    elif occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    return occurred, entry.kind, entry.event_id


class ExecutionTimelineStore(Protocol):
    """Durable append-only storage for normalized timeline entries."""

    async def append(self, run_id: str, entry: TimelineEntry) -> bool: ...

    async def append_many(self, run_id: str, entries: Sequence[TimelineEntry]) -> int: ...

    async def list(self, run_id: str) -> Sequence[TimelineEntry]: ...

    async def purge_before(self, cutoff: datetime) -> int: ...


class InMemoryExecutionTimelineStore:
    """Deterministic adapter used by projection and contract tests."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, TimelineEntry]] = {}
        self._lock = asyncio.Lock()

    async def append(self, run_id: str, entry: TimelineEntry) -> bool:
        async with self._lock:
            bucket = self._entries.setdefault(str(run_id), {})
            if entry.event_id in bucket:
                return False
            bucket[entry.event_id] = entry
            return True

    async def append_many(self, run_id: str, entries: Sequence[TimelineEntry]) -> int:
        added = 0
        async with self._lock:
            bucket = self._entries.setdefault(str(run_id), {})
            for entry in entries:
                if entry.event_id in bucket:
                    continue
                bucket[entry.event_id] = entry
                added += 1
        return added

    async def list(self, run_id: str) -> Sequence[TimelineEntry]:
        async with self._lock:
            return tuple(sorted(self._entries.get(str(run_id), {}).values(), key=_sort_key))

    async def purge_before(self, cutoff: datetime) -> int:
        removed = 0
        async with self._lock:
            for run_id, bucket in list(self._entries.items()):
                stale = [
                    event_id
                    for event_id, entry in bucket.items()
                    if entry.occurred_at is not None and entry.occurred_at < cutoff
                ]
                for event_id in stale:
                    del bucket[event_id]
                    removed += 1
                if not bucket:
                    self._entries.pop(run_id, None)
        return removed


class RedisExecutionTimelineStore:
    """Redis adapter with atomic, idempotent appends.

    Entries are kept in a per-run list for inexpensive reads and in an event
    key for deduplication.  Ordering is applied after decoding, so retries and
    out-of-order writers produce the same projection.
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    @staticmethod
    def _run_key(run_id: str) -> str:
        return f"{_PREFIX}{run_id}"

    @staticmethod
    def _event_key(run_id: str, event_id: str) -> str:
        return f"{_EVENT_PREFIX}{run_id}:{event_id}"

    async def append(self, run_id: str, entry: TimelineEntry) -> bool:
        redis = await self._client()
        event_key = self._event_key(run_id, entry.event_id)
        encoded = entry.model_dump_json()
        # A Lua script makes the idempotency marker and per-run append one
        # atomic Redis operation.  There is no crash window in which SETNX can
        # commit while RPUSH is lost, and concurrent retries return exactly
        # one successful append.
        created = await redis.eval(
            _APPEND_SCRIPT,
            2,
            event_key,
            self._run_key(run_id),
            encoded,
        )
        return bool(created)

    async def append_many(self, run_id: str, entries: Sequence[TimelineEntry]) -> int:
        added = 0
        for entry in entries:
            if await self.append(run_id, entry):
                added += 1
        return added

    async def list(self, run_id: str) -> Sequence[TimelineEntry]:
        redis = await self._client()
        values = await redis.lrange(self._run_key(run_id), 0, -1)
        decoded = []
        for value in values:
            if isinstance(value, bytes):
                value = value.decode()
            decoded.append(TimelineEntry.model_validate_json(value))
        # A writer may append a later event first; sorting is the read contract.
        return tuple(sorted(decoded, key=_sort_key))

    async def purge_before(self, cutoff: datetime) -> int:
        redis = await self._client()
        cursor: int | bytes = 0
        removed = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=f"{_PREFIX}*", count=100)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                if key.startswith(_EVENT_PREFIX):
                    continue
                run_id = key[len(_PREFIX) :]
                entries = await self.list(run_id)
                keep = [entry for entry in entries if entry.occurred_at is None or entry.occurred_at >= cutoff]
                if len(keep) == len(entries):
                    continue
                await redis.delete(key)
                for entry in entries:
                    await redis.delete(self._event_key(run_id, entry.event_id))
                if keep:
                    await self.append_many(run_id, keep)
                removed += len(entries) - len(keep)
            if cursor in {0, b"0", "0"}:
                break
        return removed


# Short names make the adapter easy to discover without breaking the explicit
# class names used in architecture documentation.
InMemoryTimelineStore = InMemoryExecutionTimelineStore
RedisTimelineStore = RedisExecutionTimelineStore
TimelineStore = ExecutionTimelineStore


def timeline_entry(
    *,
    event_id: str,
    kind: str,
    occurred_at: datetime | None,
    summary: str,
    status: str | None = None,
    details: Mapping[str, JsonValue] | None = None,
) -> TimelineEntry:
    """Build a normalized record for producers outside the read projection."""
    return TimelineEntry(
        event_id=event_id,
        kind=kind,
        occurred_at=occurred_at,
        status=status,
        summary=summary,
        details=dict(details or {}),
    )


__all__ = [
    "ExecutionTimelineStore",
    "InMemoryExecutionTimelineStore",
    "RedisExecutionTimelineStore",
    "InMemoryTimelineStore",
    "RedisTimelineStore",
    "TimelineStore",
    "timeline_entry",
]
