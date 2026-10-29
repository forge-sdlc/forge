"""Durable effect journal implementations with attempt-token fencing."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from redis.exceptions import WatchError

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.models import EffectRecord, EffectRecordStatus
from forge.orchestrator.checkpointer import get_redis_client

_RECORD_PREFIX = "forge:effects:record:"
_DUE_KEY = "forge:effects:due"
_WORKFLOW_PREFIX = "forge:effects:workflow:"

_SUBMIT_SCRIPT = """
if #KEYS == 4 and ARGV[4] and ARGV[4] ~= '' then
  local owner = redis.call('GET', KEYS[4])
  if owner ~= ARGV[4] then return -1 end
end
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[3])
return 1
"""


class EffectClaimLostError(RuntimeError):
    """The attempt token no longer owns the effect record."""


class EffectJournal(Protocol):
    @property
    def lease_duration(self) -> timedelta: ...

    async def submit(self, command: EffectCommand) -> EffectRecord: ...

    async def get(self, idempotency_key: str) -> EffectRecord | None: ...

    async def list_for_workflow(self, run_id: str) -> Sequence[EffectRecord]: ...

    async def claim_due(self, limit: int = 1) -> Sequence[EffectRecord]: ...

    async def claim(self, idempotency_key: str) -> EffectRecord | None: ...

    async def renew(self, idempotency_key: str, claim_token: str) -> EffectRecord | None: ...

    async def complete(self, result: EffectResult, claim_token: str) -> EffectRecord: ...

    async def retry(
        self, result: EffectResult, delay: timedelta, claim_token: str
    ) -> EffectRecord: ...

    async def replay(self, idempotency_key: str) -> EffectRecord: ...

    async def reconcile(
        self,
        idempotency_key: str,
        *,
        applied: bool,
        note: str,
        provider_reference: str | None = None,
    ) -> EffectRecord: ...

    async def purge_terminal_before(self, cutoff: datetime) -> int: ...


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


_CLAIMABLE = {
    EffectRecordStatus.PENDING,
    EffectRecordStatus.RETRYABLE_FAILURE,
    EffectRecordStatus.RUNNING,
}
_TERMINAL = {
    EffectRecordStatus.SUCCEEDED,
    EffectRecordStatus.PRECONDITION_FAILED,
    EffectRecordStatus.TERMINAL_FAILURE,
}


class InMemoryEffectJournal:
    """Deterministic journal for local execution and contract tests."""

    def __init__(self, *, lease: timedelta = timedelta(minutes=5)) -> None:
        self._records: dict[str, EffectRecord] = {}
        self._lock = asyncio.Lock()
        self._lease = lease

    @property
    def lease_duration(self) -> timedelta:
        return self._lease

    async def submit(self, command: EffectCommand) -> EffectRecord:
        # This check is the in-memory equivalent of the Redis script fence.
        # In-memory tests cannot participate in the distributed lock's
        # transaction, but they still reject already-stale owners.
        from forge.orchestrator.ownership import assert_ticket_ownership

        await assert_ticket_ownership()
        async with self._lock:
            existing = self._records.get(command.idempotency_key)
            if existing:
                return existing
            record = _pending(command, _now())
            self._records[command.idempotency_key] = record
            return record

    async def get(self, idempotency_key: str) -> EffectRecord | None:
        async with self._lock:
            return self._records.get(idempotency_key)

    async def list_for_workflow(self, run_id: str) -> Sequence[EffectRecord]:
        async with self._lock:
            return [
                record
                for record in self._records.values()
                if record.command.workflow.run_id == run_id
            ]

    async def claim_due(self, limit: int = 1) -> Sequence[EffectRecord]:
        now = _now()
        claimed: list[EffectRecord] = []
        async with self._lock:
            due = [
                record
                for record in self._records.values()
                if record.status in _CLAIMABLE
                and record.next_attempt_at <= now
                and (record.lease_until is None or record.lease_until <= now)
            ][:limit]
            for record in due:
                updated = self._new_claim(record, now)
                self._records[record.command.idempotency_key] = updated
                claimed.append(updated)
        return claimed

    async def claim(self, idempotency_key: str) -> EffectRecord | None:
        now = _now()
        async with self._lock:
            record = self._records.get(idempotency_key)
            if (
                record is None
                or record.status not in _CLAIMABLE
                or record.next_attempt_at > now
                or (record.lease_until is not None and record.lease_until > now)
            ):
                return None
            updated = self._new_claim(record, now)
            self._records[idempotency_key] = updated
            return updated

    def _new_claim(self, record: EffectRecord, now: datetime) -> EffectRecord:
        lease_until = now + self._lease
        return record.model_copy(
            update={
                "status": EffectRecordStatus.RUNNING,
                "attempt": record.attempt + 1,
                "updated_at": now,
                "lease_until": lease_until,
                "next_attempt_at": lease_until,
                "claim_token": uuid4().hex,
                "recovered_after_expiry": record.status is EffectRecordStatus.RUNNING,
            }
        )

    async def renew(self, idempotency_key: str, claim_token: str) -> EffectRecord | None:
        now = _now()
        async with self._lock:
            record = self._records.get(idempotency_key)
            if (
                record is None
                or record.status is not EffectRecordStatus.RUNNING
                or record.claim_token != claim_token
                or record.lease_until is None
                or record.lease_until <= now
            ):
                return None
            updated = record.model_copy(
                update={
                    "updated_at": now,
                    "lease_until": now + self._lease,
                    "next_attempt_at": now + self._lease,
                }
            )
            self._records[idempotency_key] = updated
            return updated

    async def complete(self, result: EffectResult, claim_token: str) -> EffectRecord:
        return await self._store_result(result, claim_token, delay=None)

    async def retry(self, result: EffectResult, delay: timedelta, claim_token: str) -> EffectRecord:
        return await self._store_result(result, claim_token, delay=delay)

    async def _store_result(
        self, result: EffectResult, claim_token: str, delay: timedelta | None
    ) -> EffectRecord:
        async with self._lock:
            record = self._records.get(result.idempotency_key)
            if record is None:
                raise KeyError(result.idempotency_key)
            if record.status is not EffectRecordStatus.RUNNING or record.claim_token != claim_token:
                raise EffectClaimLostError(result.idempotency_key)
            if record.lease_until is None or record.lease_until <= _now():
                raise EffectClaimLostError(result.idempotency_key)
            status = EffectRecordStatus(result.status.value)
            updated = record.model_copy(
                update={
                    "status": status,
                    "updated_at": result.completed_at,
                    "next_attempt_at": result.completed_at + (delay or timedelta()),
                    "lease_until": None,
                    "claim_token": None,
                    "result": result,
                    "attempt_history": [*record.attempt_history, result],
                }
            )
            self._records[result.idempotency_key] = updated
            return updated

    async def replay(self, idempotency_key: str) -> EffectRecord:
        async with self._lock:
            record = self._records.get(idempotency_key)
            if record is None:
                raise KeyError(idempotency_key)
            if (
                record.status
                not in {
                    EffectRecordStatus.PRECONDITION_FAILED,
                    EffectRecordStatus.TERMINAL_FAILURE,
                }
                or record.claim_token is not None
            ):
                raise ValueError(f"Effect {idempotency_key} is not replayable")
            now = _now()
            updated = record.model_copy(
                update={
                    "status": EffectRecordStatus.PENDING,
                    "updated_at": now,
                    "next_attempt_at": now,
                    "lease_until": None,
                    "claim_token": None,
                    "result": None,
                    "replay_count": record.replay_count + 1,
                }
            )
            self._records[idempotency_key] = updated
            return updated

    async def reconcile(
        self,
        idempotency_key: str,
        *,
        applied: bool,
        note: str,
        provider_reference: str | None = None,
    ) -> EffectRecord:
        from forge.effects.models import EffectReconciliation, EffectReconciliationDecision

        async with self._lock:
            record = self._records.get(idempotency_key)
            if record is None:
                raise KeyError(idempotency_key)
            if (
                record.status is not EffectRecordStatus.INDETERMINATE
                or record.claim_token is not None
            ):
                raise ValueError(f"Effect {idempotency_key} is not awaiting reconciliation")
            now = _now()
            decision = (
                EffectReconciliationDecision.APPLIED
                if applied
                else EffectReconciliationDecision.NOT_APPLIED
            )
            reconciliation = EffectReconciliation(
                decision=decision,
                occurred_at=now,
                note=note,
                provider_reference=provider_reference,
            )
            result = (
                EffectResult(
                    effect_id=record.command.effect_id,
                    idempotency_key=idempotency_key,
                    status=EffectResultStatus.SUCCEEDED,
                    completed_at=now,
                    provider_reference=provider_reference,
                    output={"reconciled": True, "note": note},
                )
                if applied
                else None
            )
            updated = record.model_copy(
                update={
                    "status": (
                        EffectRecordStatus.SUCCEEDED if applied else EffectRecordStatus.PENDING
                    ),
                    "updated_at": now,
                    "next_attempt_at": now,
                    "lease_until": None,
                    "claim_token": None,
                    "result": result,
                    "replay_count": record.replay_count + (0 if applied else 1),
                    "reconciliation_history": [*record.reconciliation_history, reconciliation],
                }
            )
            self._records[idempotency_key] = updated
            return updated

    async def purge_terminal_before(self, cutoff: datetime) -> int:
        async with self._lock:
            keys = [
                key
                for key, record in self._records.items()
                if record.status in _TERMINAL
                and record.claim_token is None
                and record.updated_at < cutoff
            ]
            for key in keys:
                del self._records[key]
            return len(keys)


class RedisEffectJournal:
    """Redis journal using WATCH/MULTI for token-fenced record/index updates."""

    def __init__(
        self,
        redis_client: Any = None,
        *,
        lease: timedelta = timedelta(minutes=5),
    ) -> None:
        self._redis = redis_client
        self._lease = lease

    @property
    def lease_duration(self) -> timedelta:
        return self._lease

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def submit(self, command: EffectCommand) -> EffectRecord:
        redis = await self._client()
        now = _now()
        record = _pending(command, now)
        from forge.orchestrator.ownership import TicketOwnershipLost, get_ticket_ownership

        owner = get_ticket_ownership()
        keys = [
            f"{_RECORD_PREFIX}{command.idempotency_key}",
            _DUE_KEY,
            f"{_WORKFLOW_PREFIX}{command.workflow.run_id}",
        ]
        args = [record.model_dump_json(), now.timestamp(), command.idempotency_key]
        if owner is not None:
            keys.append(owner.key)
            owner_token = (
                owner.token.decode() if isinstance(owner.token, bytes) else str(owner.token)
            )
            args.append(owner_token)
        result = await redis.eval(
            _SUBMIT_SCRIPT,
            len(keys),
            *keys,
            *args,
        )
        if result == -1:
            raise TicketOwnershipLost(f"Ticket ownership lost: {owner.key if owner else ''}")
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

    async def claim_due(self, limit: int = 1) -> Sequence[EffectRecord]:
        redis = await self._client()
        now = _now()
        # Inspect a small batch beyond the requested claim count so stale index
        # entries cannot hold the first slot and starve valid due effects.
        members = await redis.zrangebyscore(
            _DUE_KEY, "-inf", now.timestamp(), start=0, num=max(limit * 10, 100)
        )
        claimed: list[EffectRecord] = []
        for raw in members:
            key = raw.decode() if isinstance(raw, bytes) else raw
            record = await self._claim_member(redis, key)
            if record is not None:
                claimed.append(record)
        return claimed

    async def _claim_member(self, redis: Any, idempotency_key: str) -> EffectRecord | None:
        key = f"{_RECORD_PREFIX}{idempotency_key}"
        for _ in range(5):
            attempt_now = _now()
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        pipe.multi()
                        pipe.zrem(_DUE_KEY, idempotency_key)
                        await pipe.execute()
                        return None
                    record = EffectRecord.model_validate_json(raw)
                    if record.status not in _CLAIMABLE:
                        pipe.multi()
                        pipe.zrem(_DUE_KEY, idempotency_key)
                        await pipe.execute()
                        return None
                    due_at = max(
                        record.next_attempt_at, record.lease_until or record.next_attempt_at
                    )
                    if due_at > attempt_now:
                        pipe.multi()
                        pipe.zadd(_DUE_KEY, {idempotency_key: due_at.timestamp()})
                        await pipe.execute()
                        return None
                    lease_until = attempt_now + self._lease
                    updated = record.model_copy(
                        update={
                            "status": EffectRecordStatus.RUNNING,
                            "attempt": record.attempt + 1,
                            "updated_at": attempt_now,
                            "lease_until": lease_until,
                            "next_attempt_at": lease_until,
                            "claim_token": uuid4().hex,
                            "recovered_after_expiry": record.status is EffectRecordStatus.RUNNING,
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    pipe.zadd(_DUE_KEY, {idempotency_key: lease_until.timestamp()})
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        return None

    async def claim(self, idempotency_key: str) -> EffectRecord | None:
        redis = await self._client()
        return await self._claim_member(redis, idempotency_key)

    async def renew(self, idempotency_key: str, claim_token: str) -> EffectRecord | None:
        redis = await self._client()
        key = f"{_RECORD_PREFIX}{idempotency_key}"
        for _ in range(5):
            now = _now()
            lease_until = now + self._lease
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        return None
                    record = EffectRecord.model_validate_json(raw)
                    if (
                        record.status is not EffectRecordStatus.RUNNING
                        or record.claim_token != claim_token
                        or record.lease_until is None
                        or record.lease_until <= now
                    ):
                        return None
                    updated = record.model_copy(
                        update={
                            "updated_at": now,
                            "lease_until": lease_until,
                            "next_attempt_at": lease_until,
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    pipe.zadd(_DUE_KEY, {idempotency_key: lease_until.timestamp()})
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        return None

    async def complete(self, result: EffectResult, claim_token: str) -> EffectRecord:
        return await self._store_result(result, claim_token, delay=None)

    async def retry(self, result: EffectResult, delay: timedelta, claim_token: str) -> EffectRecord:
        return await self._store_result(result, claim_token, delay=delay)

    async def _store_result(
        self, result: EffectResult, claim_token: str, delay: timedelta | None
    ) -> EffectRecord:
        redis = await self._client()
        next_attempt = result.completed_at + (delay or timedelta())
        key = f"{_RECORD_PREFIX}{result.idempotency_key}"
        for _ in range(5):
            now = _now()
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        raise KeyError(result.idempotency_key)
                    record = EffectRecord.model_validate_json(raw)
                    if (
                        record.status is not EffectRecordStatus.RUNNING
                        or record.claim_token != claim_token
                        or record.lease_until is None
                        or record.lease_until <= now
                    ):
                        raise EffectClaimLostError(result.idempotency_key)
                    updated = record.model_copy(
                        update={
                            "status": EffectRecordStatus(result.status.value),
                            "updated_at": result.completed_at,
                            "next_attempt_at": next_attempt,
                            "lease_until": None,
                            "claim_token": None,
                            "result": result,
                            "attempt_history": [*record.attempt_history, result],
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    if result.status is EffectResultStatus.RETRYABLE_FAILURE:
                        pipe.zadd(_DUE_KEY, {result.idempotency_key: next_attempt.timestamp()})
                    else:
                        pipe.zrem(_DUE_KEY, result.idempotency_key)
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        raise EffectClaimLostError(result.idempotency_key)

    async def replay(self, idempotency_key: str) -> EffectRecord:
        redis = await self._client()
        key = f"{_RECORD_PREFIX}{idempotency_key}"
        for _ in range(5):
            now = _now()
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        raise KeyError(idempotency_key)
                    record = EffectRecord.model_validate_json(raw)
                    if (
                        record.status
                        not in {
                            EffectRecordStatus.PRECONDITION_FAILED,
                            EffectRecordStatus.TERMINAL_FAILURE,
                        }
                        or record.claim_token is not None
                    ):
                        raise ValueError(f"Effect {idempotency_key} is not replayable")
                    updated = record.model_copy(
                        update={
                            "status": EffectRecordStatus.PENDING,
                            "updated_at": now,
                            "next_attempt_at": now,
                            "lease_until": None,
                            "claim_token": None,
                            "result": None,
                            "replay_count": record.replay_count + 1,
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    pipe.zadd(_DUE_KEY, {idempotency_key: now.timestamp()})
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        raise ValueError(f"Effect {idempotency_key} changed repeatedly during replay")

    async def reconcile(
        self,
        idempotency_key: str,
        *,
        applied: bool,
        note: str,
        provider_reference: str | None = None,
    ) -> EffectRecord:
        from forge.effects.models import EffectReconciliation, EffectReconciliationDecision

        redis = await self._client()
        key = f"{_RECORD_PREFIX}{idempotency_key}"
        for _ in range(5):
            now = _now()
            async with redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        raise KeyError(idempotency_key)
                    record = EffectRecord.model_validate_json(raw)
                    if record.status is not EffectRecordStatus.INDETERMINATE or record.claim_token:
                        raise ValueError(f"Effect {idempotency_key} is not awaiting reconciliation")
                    decision = (
                        EffectReconciliationDecision.APPLIED
                        if applied
                        else EffectReconciliationDecision.NOT_APPLIED
                    )
                    reconciliation = EffectReconciliation(
                        decision=decision,
                        occurred_at=now,
                        note=note,
                        provider_reference=provider_reference,
                    )
                    result = (
                        EffectResult(
                            effect_id=record.command.effect_id,
                            idempotency_key=idempotency_key,
                            status=EffectResultStatus.SUCCEEDED,
                            completed_at=now,
                            provider_reference=provider_reference,
                            output={"reconciled": True, "note": note},
                        )
                        if applied
                        else None
                    )
                    updated = record.model_copy(
                        update={
                            "status": EffectRecordStatus.SUCCEEDED
                            if applied
                            else EffectRecordStatus.PENDING,
                            "updated_at": now,
                            "next_attempt_at": now,
                            "lease_until": None,
                            "claim_token": None,
                            "result": result,
                            "replay_count": record.replay_count + (0 if applied else 1),
                            "reconciliation_history": [
                                *record.reconciliation_history,
                                reconciliation,
                            ],
                        }
                    )
                    pipe.multi()
                    pipe.set(key, updated.model_dump_json())
                    if applied:
                        pipe.zrem(_DUE_KEY, idempotency_key)
                    else:
                        pipe.zadd(_DUE_KEY, {idempotency_key: now.timestamp()})
                    await pipe.execute()
                    return updated
                except WatchError:
                    continue
        raise ValueError(f"Effect {idempotency_key} changed repeatedly during reconciliation")

    async def purge_terminal_before(self, cutoff: datetime) -> int:
        redis = await self._client()
        cursor: int | bytes = 0
        removed = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=f"{_RECORD_PREFIX}*", count=100)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                for _ in range(3):
                    async with redis.pipeline(transaction=True) as pipe:
                        try:
                            await pipe.watch(key)
                            value = await pipe.get(key)
                            if not value:
                                break
                            record = EffectRecord.model_validate_json(value)
                            if (
                                record.status not in _TERMINAL
                                or record.claim_token is not None
                                or record.updated_at >= cutoff
                            ):
                                break
                            identity = record.command.idempotency_key
                            workflow_key = f"{_WORKFLOW_PREFIX}{record.command.workflow.run_id}"
                            await pipe.watch(_DUE_KEY, workflow_key)
                            pipe.multi()
                            pipe.delete(key)
                            pipe.zrem(_DUE_KEY, identity)
                            pipe.srem(workflow_key, identity)
                            await pipe.execute()
                            removed += 1
                            break
                        except WatchError:
                            continue
            if cursor in {0, b"0", "0"}:
                break
        return removed
