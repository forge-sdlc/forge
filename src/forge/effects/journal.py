"""Durable effect journal implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.models import EffectRecord, EffectRecordStatus
from forge.orchestrator.checkpointer import get_redis_client

_RECORD_PREFIX = "forge:effects:record:"
_DUE_KEY = "forge:effects:due"
_WORKFLOW_PREFIX = "forge:effects:workflow:"

_SUBMIT_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[3])
return 1
"""

_CLAIM_SCRIPT = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[3])
for _, member in ipairs(members) do
  redis.call('ZADD', KEYS[1], ARGV[2], member)
end
return members
"""


class EffectJournal(Protocol):
    async def submit(self, command: EffectCommand) -> EffectRecord: ...

    async def get(self, idempotency_key: str) -> EffectRecord | None: ...

    async def list_for_workflow(self, run_id: str) -> Sequence[EffectRecord]: ...

    async def claim_due(self, limit: int = 10) -> Sequence[EffectRecord]: ...

    async def complete(self, result: EffectResult) -> EffectRecord: ...

    async def retry(self, result: EffectResult, delay: timedelta) -> EffectRecord: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _pending(command: EffectCommand, now: datetime) -> EffectRecord:
    return EffectRecord(
        command=command,
        status=EffectRecordStatus.PENDING,
        attempt=0,
        created_at=now,
        updated_at=now,
        next_attempt_at=now,
    )


class InMemoryEffectJournal:
    """Deterministic journal for local execution and contract tests."""

    def __init__(self, *, lease: timedelta = timedelta(minutes=5)) -> None:
        self._records: dict[str, EffectRecord] = {}
        self._lock = asyncio.Lock()
        self._lease = lease

    async def submit(self, command: EffectCommand) -> EffectRecord:
        async with self._lock:
            existing = self._records.get(command.idempotency_key)
            if existing:
                return existing
            record = _pending(command, _now())
            self._records[command.idempotency_key] = record
            return record

    async def get(self, idempotency_key: str) -> EffectRecord | None:
        return self._records.get(idempotency_key)

    async def list_for_workflow(self, run_id: str) -> Sequence[EffectRecord]:
        return [
            record for record in self._records.values() if record.command.workflow.run_id == run_id
        ]

    async def claim_due(self, limit: int = 10) -> Sequence[EffectRecord]:
        now = _now()
        async with self._lock:
            due = [
                record
                for record in self._records.values()
                if record.status
                in {
                    EffectRecordStatus.PENDING,
                    EffectRecordStatus.RETRYABLE_FAILURE,
                    EffectRecordStatus.RUNNING,
                }
                and record.next_attempt_at <= now
                and (record.lease_until is None or record.lease_until <= now)
            ][:limit]
            claimed = []
            for record in due:
                updated = record.model_copy(
                    update={
                        "status": EffectRecordStatus.RUNNING,
                        "attempt": record.attempt + 1,
                        "updated_at": now,
                        "lease_until": now + self._lease,
                    }
                )
                self._records[record.command.idempotency_key] = updated
                claimed.append(updated)
            return claimed

    async def complete(self, result: EffectResult) -> EffectRecord:
        return await self._store_result(result, delay=None)

    async def retry(self, result: EffectResult, delay: timedelta) -> EffectRecord:
        return await self._store_result(result, delay=delay)

    async def _store_result(self, result: EffectResult, delay: timedelta | None) -> EffectRecord:
        async with self._lock:
            record = self._records[result.idempotency_key]
            status = EffectRecordStatus(result.status.value)
            updated = record.model_copy(
                update={
                    "status": status,
                    "updated_at": result.completed_at,
                    "next_attempt_at": result.completed_at + (delay or timedelta()),
                    "lease_until": None,
                    "result": result,
                }
            )
            self._records[result.idempotency_key] = updated
            return updated


class RedisEffectJournal:
    """Redis-backed journal with atomic submission and exclusive leases."""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        lease: timedelta = timedelta(minutes=5),
    ) -> None:
        self._redis = redis_client
        self._lease = lease

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def submit(self, command: EffectCommand) -> EffectRecord:
        redis = await self._client()
        now = _now()
        record = _pending(command, now)
        key = f"{_RECORD_PREFIX}{command.idempotency_key}"
        await redis.eval(
            _SUBMIT_SCRIPT,
            3,
            key,
            _DUE_KEY,
            f"{_WORKFLOW_PREFIX}{command.workflow.run_id}",
            record.model_dump_json(),
            now.timestamp(),
            command.idempotency_key,
        )
        stored = await self.get(command.idempotency_key)
        assert stored is not None
        return stored

    async def get(self, idempotency_key: str) -> EffectRecord | None:
        redis = await self._client()
        value = await redis.get(f"{_RECORD_PREFIX}{idempotency_key}")
        return EffectRecord.model_validate_json(value) if value else None

    async def list_for_workflow(self, run_id: str) -> Sequence[EffectRecord]:
        redis = await self._client()
        members = await redis.smembers(f"{_WORKFLOW_PREFIX}{run_id}")
        records = []
        for raw in members:
            key = raw.decode() if isinstance(raw, bytes) else raw
            record = await self.get(key)
            if record is not None:
                records.append(record)
        return records

    async def claim_due(self, limit: int = 10) -> Sequence[EffectRecord]:
        redis = await self._client()
        now = _now()
        lease_until = now + self._lease
        members = await redis.eval(
            _CLAIM_SCRIPT,
            1,
            _DUE_KEY,
            now.timestamp(),
            lease_until.timestamp(),
            limit,
        )
        claimed = []
        for raw in members:
            idempotency_key = raw.decode() if isinstance(raw, bytes) else raw
            record = await self.get(idempotency_key)
            if record is None:
                await redis.zrem(_DUE_KEY, idempotency_key)
                continue
            if record.status not in {
                EffectRecordStatus.PENDING,
                EffectRecordStatus.RETRYABLE_FAILURE,
                EffectRecordStatus.RUNNING,
            }:
                await redis.zrem(_DUE_KEY, idempotency_key)
                continue
            updated = record.model_copy(
                update={
                    "status": EffectRecordStatus.RUNNING,
                    "attempt": record.attempt + 1,
                    "updated_at": now,
                    "lease_until": lease_until,
                    "next_attempt_at": lease_until,
                }
            )
            await redis.set(f"{_RECORD_PREFIX}{idempotency_key}", updated.model_dump_json())
            claimed.append(updated)
        return claimed

    async def complete(self, result: EffectResult) -> EffectRecord:
        return await self._store_result(result, delay=None)

    async def retry(self, result: EffectResult, delay: timedelta) -> EffectRecord:
        return await self._store_result(result, delay=delay)

    async def _store_result(self, result: EffectResult, delay: timedelta | None) -> EffectRecord:
        redis = await self._client()
        record = await self.get(result.idempotency_key)
        if record is None:
            raise KeyError(result.idempotency_key)
        next_attempt = result.completed_at + (delay or timedelta())
        updated = record.model_copy(
            update={
                "status": EffectRecordStatus(result.status.value),
                "updated_at": result.completed_at,
                "next_attempt_at": next_attempt,
                "lease_until": None,
                "result": result,
            }
        )
        key = f"{_RECORD_PREFIX}{result.idempotency_key}"
        pipeline = redis.pipeline(transaction=True)
        pipeline.set(key, updated.model_dump_json())
        if result.status is EffectResultStatus.RETRYABLE_FAILURE:
            pipeline.zadd(_DUE_KEY, {result.idempotency_key: next_attempt.timestamp()})
        else:
            pipeline.zrem(_DUE_KEY, result.idempotency_key)
        await pipeline.execute()
        return updated
