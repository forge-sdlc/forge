"""Effect ownership contract tests against the dedicated Redis DB 3."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from forge.domain import (
    EffectCommand,
    EffectResult,
    EffectResultStatus,
    ResourceIdentity,
    WorkflowIdentity,
)
from forge.effects import (
    EffectClaimLostError,
    EffectExecutorRegistry,
    EffectService,
    RedisEffectJournal,
)
from forge.orchestrator.ownership import (
    TicketOwnership,
    TicketOwnershipLost,
    ownership_scope,
)


def _command(key: str | None = None, *, payload: dict | None = None) -> EffectCommand:
    identity = key or f"effect-test:{uuid4().hex}"
    return EffectCommand(
        effect_id=identity,
        idempotency_key=identity,
        workflow=WorkflowIdentity(
            run_id=f"EFFECT-TEST-{uuid4().hex}", workflow_name="test", definition_revision=1
        ),
        operation="test.write",
        target=ResourceIdentity(resource_type="issue", external_id="EFFECT-TEST-1"),
        payload={} if payload is None else payload,
    )


def _result(command: EffectCommand, status: EffectResultStatus = EffectResultStatus.SUCCEEDED):
    return EffectResult(
        effect_id=command.effect_id,
        idempotency_key=command.idempotency_key,
        status=status,
        completed_at=datetime.now(UTC),
        output={"empty_array": [], "empty_object": {}, "nested": {"items": []}},
    )


async def _cleanup(client, command: EffectCommand) -> None:
    await client.delete(f"forge:effects:record:{command.idempotency_key}")
    await client.zrem("forge:effects:due", command.idempotency_key)
    await client.srem(f"forge:effects:workflow:{command.workflow.run_id}", command.idempotency_key)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_is_fenced_by_ticket_ownership(redis_client) -> None:
    command = _command()
    journal = RedisEffectJournal(redis_client)
    lock_key = "forge:ticket:ownership:effect-submit-test"
    try:
        await redis_client.set(lock_key, "current-owner")
        with (
            ownership_scope(TicketOwnership(redis_client, lock_key, "stale-owner")),
            pytest.raises(TicketOwnershipLost),
        ):
            await journal.submit(command)
        assert await journal.get(command.idempotency_key) is None
        assert await redis_client.zscore("forge:effects:due", command.idempotency_key) is None
        assert (
            await redis_client.sismember(
                f"forge:effects:workflow:{command.workflow.run_id}",
                command.idempotency_key,
            )
            == 0
        )

        with ownership_scope(TicketOwnership(redis_client, lock_key, "current-owner")):
            submitted = await journal.submit(command)
        assert submitted.command == command
        assert await redis_client.zscore("forge:effects:due", command.idempotency_key) is not None
    finally:
        await _cleanup(redis_client, command)
        await redis_client.delete(lock_key)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_services_have_one_claim_and_stale_owner_cannot_commit(
    redis_client,
) -> None:
    command = _command()
    first_journal = RedisEffectJournal(redis_client, lease=timedelta(milliseconds=80))
    second_journal = RedisEffectJournal(redis_client, lease=timedelta(milliseconds=80))
    try:
        await first_journal.submit(command)
        claimed = await asyncio.gather(
            first_journal.claim(command.idempotency_key),
            second_journal.claim(command.idempotency_key),
        )
        first = next(item for item in claimed if item is not None)
        assert sum(item is not None for item in claimed) == 1
        await asyncio.sleep(0.1)
        successor = await second_journal.claim(command.idempotency_key)
        assert successor is not None and successor.claim_token != first.claim_token
        with pytest.raises(EffectClaimLostError):
            await first_journal.complete(_result(command), first.claim_token or "")
        with pytest.raises(EffectClaimLostError):
            await first_journal.retry(
                _result(command, EffectResultStatus.RETRYABLE_FAILURE),
                timedelta(0),
                first.claim_token or "",
            )
        completed = await second_journal.complete(_result(command), successor.claim_token or "")
        assert completed.status.value == "succeeded"
    finally:
        await _cleanup(redis_client, command)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_long_running_claim_renews_and_json_empty_arrays_round_trip(redis_client) -> None:
    command = _command(payload={"empty_array": [], "empty_object": {}, "nested": {"values": []}})
    journal_a = RedisEffectJournal(redis_client, lease=timedelta(milliseconds=150))
    journal_b = RedisEffectJournal(redis_client, lease=timedelta(milliseconds=150))
    try:
        await journal_a.submit(command)
        claimed = await journal_a.claim(command.idempotency_key)
        assert claimed is not None
        assert claimed.command.payload == command.payload
        renewed = await journal_a.renew(command.idempotency_key, claimed.claim_token or "")
        assert renewed is not None
        assert (await journal_b.claim(command.idempotency_key)) is None
        result = await journal_a.complete(_result(command), claimed.claim_token or "")
        assert result.result is not None
        assert result.result.output == {
            "empty_array": [],
            "empty_object": {},
            "nested": {"items": []},
        }
        assert result.command.payload == {
            "empty_array": [],
            "empty_object": {},
            "nested": {"values": []},
        }
    finally:
        await _cleanup(redis_client, command)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_redis_claim_cannot_renew_or_complete(redis_client) -> None:
    command = _command()
    journal = RedisEffectJournal(redis_client, lease=timedelta(milliseconds=80))
    try:
        await journal.submit(command)
        claim = await journal.claim(command.idempotency_key)
        assert claim is not None and claim.claim_token
        await asyncio.sleep(0.12)

        assert await journal.renew(command.idempotency_key, claim.claim_token) is None
        with pytest.raises(EffectClaimLostError):
            await journal.complete(_result(command), claim.claim_token)
    finally:
        await _cleanup(redis_client, command)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_orphan_due_index_entry_does_not_starve_valid_effect(redis_client) -> None:
    command = _command()
    journal = RedisEffectJournal(redis_client)
    orphan = f"orphan:{uuid4().hex}"
    try:
        await redis_client.zadd("forge:effects:due", {orphan: datetime.now(UTC).timestamp() - 1})
        await journal.submit(command)

        claimed = await journal.claim_due(1)

        assert len(claimed) == 1
        assert claimed[0].command.idempotency_key == command.idempotency_key
        assert await redis_client.zscore("forge:effects:due", orphan) is None
    finally:
        await _cleanup(redis_client, command)
        await redis_client.zrem("forge:effects:due", orphan)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2])
async def test_claim_due_leaves_excess_effects_unclaimed(redis_client, limit) -> None:
    journal = RedisEffectJournal(redis_client)
    commands = [_command() for _ in range(3)]
    for command in commands:
        await journal.submit(command)
    await redis_client.zadd("forge:effects:due", {"orphan": 0})

    claimed = await journal.claim_due(limit)

    assert len(claimed) == limit
    claimed_keys = {record.command.idempotency_key for record in claimed}
    for command in commands:
        record = await journal.get(command.idempotency_key)
        assert record is not None
        if command.idempotency_key not in claimed_keys:
            assert record.status.value == "pending"
            assert record.claim_token is None
            assert record.lease_until is None
            assert record.attempt == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_executes_all_due_effects_without_stranding_claims(redis_client) -> None:
    journal = RedisEffectJournal(redis_client)
    commands = [_command() for _ in range(3)]
    calls = []

    class Executor:
        operation = "test.write"
        retry_safe_after_ambiguous_failure = False

        async def execute(self, command):
            calls.append(command.idempotency_key)
            return _result(command)

    executors = EffectExecutorRegistry()
    executors.register(Executor())
    service = EffectService(journal, executors)
    for command in commands:
        await service.submit(command)

    completed = await service.run_due(limit=len(commands))

    assert len(completed) == len(commands)
    assert set(calls) == {command.idempotency_key for command in commands}
    assert all(record.status.value == "succeeded" for record in completed)
    assert all(record.attempt == 1 for record in completed)
    assert await redis_client.zcard("forge:effects:due") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_and_purge_are_serialized_with_claims(redis_client) -> None:
    command = _command()
    journal_a = RedisEffectJournal(redis_client, lease=timedelta(seconds=2))
    journal_b = RedisEffectJournal(redis_client, lease=timedelta(seconds=2))
    try:
        await journal_a.submit(command)
        claim = await journal_a.claim(command.idempotency_key)
        assert claim is not None
        failed = await journal_a.complete(
            _result(command, EffectResultStatus.TERMINAL_FAILURE), claim.claim_token or ""
        )
        # Replay may atomically return the record to pending and then be claimed,
        # or the claim may observe the pre-replay terminal state. It must never
        # overwrite a live running claim.
        replay, competing_claim = await asyncio.gather(
            journal_a.replay(command.idempotency_key),
            journal_b.claim(command.idempotency_key),
            return_exceptions=True,
        )
        current = await journal_a.get(command.idempotency_key)
        assert current is not None
        if current.status.value == "running":
            assert current.claim_token
            with pytest.raises(ValueError):
                await journal_a.replay(command.idempotency_key)
        else:
            assert current.status.value in {"pending", "terminal_failure"}
        if not isinstance(replay, Exception):
            assert replay.replay_count == failed.replay_count + 1
        if not isinstance(competing_claim, Exception):
            assert competing_claim is None or competing_claim.claim_token == current.claim_token

        # Purge checks status, cutoff and no active token inside the same WATCH
        # transaction. A live claim is therefore retained.
        pending = await journal_a.get(command.idempotency_key)
        if pending and pending.status.value == "running":
            assert await journal_a.purge_terminal_before(datetime.now(UTC) + timedelta(days=1)) == 0
            assert await journal_a.get(command.idempotency_key) is not None
    finally:
        await _cleanup(redis_client, command)
