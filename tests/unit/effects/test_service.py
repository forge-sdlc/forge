import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from forge.domain import (
    EffectCommand,
    EffectResult,
    EffectResultStatus,
    ResourceIdentity,
    WorkflowIdentity,
)
from forge.effects import (
    EffectExecutorRegistry,
    EffectRecordStatus,
    EffectService,
    InMemoryEffectJournal,
    RequiredEffectError,
)
from forge.integrations.source_control.errors import ConflictError


def _command(key: str = "same-logical-effect") -> EffectCommand:
    return EffectCommand(
        effect_id="effect-1",
        idempotency_key=key,
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        operation="test.write",
        target=ResourceIdentity(resource_type="issue", external_id="FORGE-1"),
        payload={"value": "hello"},
    )


class _Executor:
    operation = "test.write"
    retry_safe_after_ambiguous_failure = True

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, command: EffectCommand) -> EffectResult:
        self.calls += 1
        return EffectResult(
            effect_id=command.effect_id,
            idempotency_key=command.idempotency_key,
            status=EffectResultStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            provider_reference="external-1",
        )


@pytest.mark.asyncio
async def test_duplicate_submission_executes_once() -> None:
    journal = InMemoryEffectJournal()
    executor = _Executor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(journal, registry)

    first = await service.submit(_command())
    second = await service.submit(_command())
    completed = await service.run_due()

    assert first == second
    assert executor.calls == 1
    assert completed[0].status is EffectRecordStatus.SUCCEEDED
    assert (await journal.get("same-logical-effect")) == completed[0]
    assert await journal.list_for_workflow("FORGE-1") == completed


@pytest.mark.asyncio
async def test_failure_is_retried_without_rerunning_originating_station() -> None:
    class FlakyExecutor(_Executor):
        async def execute(self, command: EffectCommand) -> EffectResult:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("provider unavailable")
            return await super().execute(command)

    journal = InMemoryEffectJournal()
    executor = FlakyExecutor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(
        journal,
        registry,
        base_retry_delay=timedelta(0),
    )
    await service.submit(_command())

    first = (await service.run_due(limit=1))[0]
    second = (await service.run_due(limit=1))[0]

    assert first.status is EffectRecordStatus.RETRYABLE_FAILURE
    assert second.status is EffectRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_expired_running_lease_is_recovered() -> None:
    journal = InMemoryEffectJournal(lease=timedelta(0))
    await journal.submit(_command())

    first = (await journal.claim_due())[0]
    recovered = (await journal.claim_due())[0]

    assert first.attempt == 1
    assert recovered.attempt == 2


@pytest.mark.asyncio
async def test_execute_now_persists_claims_and_executes_exact_effect() -> None:
    journal = InMemoryEffectJournal()
    executor = _Executor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(journal, registry)

    first = await service.execute_now(_command())
    duplicate = await service.execute_now(_command())

    assert first.status is EffectRecordStatus.SUCCEEDED
    assert duplicate == first
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_claim_one_excludes_parallel_claim() -> None:
    journal = InMemoryEffectJournal()
    await journal.submit(_command())

    first = await journal.claim("same-logical-effect")
    competing = await journal.claim("same-logical-effect")

    assert first is not None
    assert competing is None


@pytest.mark.asyncio
async def test_required_effect_fails_closed_on_retryable_result() -> None:
    class FailingExecutor(_Executor):
        async def execute(self, _command: EffectCommand) -> EffectResult:
            raise TimeoutError("later")

    journal = InMemoryEffectJournal()
    registry = EffectExecutorRegistry()
    registry.register(FailingExecutor())
    service = EffectService(journal, registry)

    with pytest.raises(RequiredEffectError):
        await service.execute_required(_command())


@pytest.mark.asyncio
async def test_required_effect_waits_for_concurrent_owner_to_complete() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedExecutor(_Executor):
        async def execute(self, command: EffectCommand) -> EffectResult:
            self.calls += 1
            started.set()
            await release.wait()
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                provider_reference="external-1",
            )

    journal = InMemoryEffectJournal()
    executor = DelayedExecutor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(
        journal,
        registry,
        required_effect_wait_timeout=timedelta(seconds=1),
    )
    command = _command()
    await journal.submit(command)
    claimed = await journal.claim(command.idempotency_key)
    assert claimed is not None

    owner = asyncio.create_task(service._execute(claimed))
    await started.wait()
    waiter = asyncio.create_task(service.execute_required(command))
    await asyncio.sleep(0)
    release.set()

    record = await waiter
    await owner

    assert record.status is EffectRecordStatus.SUCCEEDED
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_attempt_history_survives_retry_and_success() -> None:
    class FlakyExecutor(_Executor):
        async def execute(self, command: EffectCommand) -> EffectResult:
            if self.calls == 0:
                self.calls += 1
                raise TimeoutError("later")
            return await super().execute(command)

    journal = InMemoryEffectJournal()
    registry = EffectExecutorRegistry()
    registry.register(FlakyExecutor())
    service = EffectService(journal, registry, base_retry_delay=timedelta(0))
    await service.submit(_command())

    await service.run_due(limit=1)
    completed = (await service.run_due(limit=1))[0]

    assert [attempt.status for attempt in completed.attempt_history] == [
        EffectResultStatus.RETRYABLE_FAILURE,
        EffectResultStatus.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_precondition_failure_requires_explicit_replay() -> None:
    class ConflictingExecutor(_Executor):
        async def execute(self, _command: EffectCommand) -> EffectResult:
            raise ConflictError("provider state changed")

    journal = InMemoryEffectJournal()
    registry = EffectExecutorRegistry()
    registry.register(ConflictingExecutor())
    service = EffectService(journal, registry)
    await service.submit(_command())

    failed = (await service.run_due())[0]
    replayed = await service.replay(_command().idempotency_key)

    assert failed.status is EffectRecordStatus.PRECONDITION_FAILED
    assert replayed.status is EffectRecordStatus.PENDING
    assert replayed.replay_count == 1
    assert len(replayed.attempt_history) == 1


@pytest.mark.asyncio
async def test_retention_only_purges_old_terminal_effects() -> None:
    journal = InMemoryEffectJournal()
    executor = _Executor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(journal, registry)
    await service.execute_now(_command("old"))
    await service.submit(_command("pending"))

    removed = await service.purge_terminal_before(datetime.now(UTC) + timedelta(seconds=1))

    assert removed == 1
    assert await journal.get("old") is None
    assert await journal.get("pending") is not None


@pytest.mark.asyncio
async def test_retention_preserves_indeterminate_effects_until_reconciled() -> None:
    class AmbiguousExecutor(_Executor):
        retry_safe_after_ambiguous_failure = False

        async def execute(self, _command: EffectCommand) -> EffectResult:
            raise TimeoutError("provider outcome unknown")

    journal = InMemoryEffectJournal()
    registry = EffectExecutorRegistry()
    registry.register(AmbiguousExecutor())
    service = EffectService(journal, registry)
    command = _command("unknown-outcome")
    await service.submit(command)
    unresolved = (await service.run_due())[0]

    removed = await service.purge_terminal_before(datetime.now(UTC) + timedelta(days=1))

    assert unresolved.status is EffectRecordStatus.INDETERMINATE
    assert removed == 0
    assert await journal.get(command.idempotency_key) == unresolved


@pytest.mark.asyncio
async def test_stale_attempt_cannot_complete_or_reschedule_after_reclaim() -> None:
    from forge.effects.journal import EffectClaimLostError

    journal = InMemoryEffectJournal(lease=timedelta(milliseconds=20))
    command = _command()
    await journal.submit(command)
    first = await journal.claim(command.idempotency_key)
    assert first is not None and first.claim_token
    await asyncio.sleep(0.03)
    second = await journal.claim(command.idempotency_key)
    assert second is not None and second.claim_token != first.claim_token
    result = EffectResult(
        effect_id=command.effect_id,
        idempotency_key=command.idempotency_key,
        status=EffectResultStatus.SUCCEEDED,
        completed_at=datetime.now(UTC),
    )

    with pytest.raises(EffectClaimLostError):
        await journal.complete(result, first.claim_token)
    with pytest.raises(EffectClaimLostError):
        await journal.retry(result, timedelta(0), first.claim_token)
    completed = await journal.complete(result, second.claim_token)
    assert completed.status is EffectRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_active_effect_renews_lease_during_long_execution() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowExecutor(_Executor):
        async def execute(self, command: EffectCommand) -> EffectResult:
            started.set()
            await release.wait()
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
            )

    journal = InMemoryEffectJournal(lease=timedelta(milliseconds=90))
    registry = EffectExecutorRegistry()
    registry.register(SlowExecutor())
    service = EffectService(journal, registry, lease_renewal_interval=0.02)
    command = _command()
    await service.submit(command)
    running = asyncio.create_task(service.run_due())
    await started.wait()
    await asyncio.sleep(0.14)
    assert await journal.claim(command.idempotency_key) is None
    release.set()
    result = (await running)[0]
    assert result.status is EffectRecordStatus.SUCCEEDED
    assert result.attempt == 1


@pytest.mark.asyncio
async def test_unknown_ambiguous_write_outcome_is_retained_for_reconciliation() -> None:
    class AmbiguousExecutor(_Executor):
        retry_safe_after_ambiguous_failure = False

        async def execute(self, _command: EffectCommand) -> EffectResult:
            raise TimeoutError("provider accepted the request but response was lost")

    journal = InMemoryEffectJournal()
    registry = EffectExecutorRegistry()
    registry.register(AmbiguousExecutor())
    service = EffectService(journal, registry)
    await service.submit(_command())

    result = (await service.run_due())[0]

    assert result.status is EffectRecordStatus.INDETERMINATE
    assert result.result is not None
    assert result.result.status is EffectResultStatus.INDETERMINATE
    assert await journal.claim_due() == []


@pytest.mark.asyncio
async def test_expired_unsafe_attempt_is_not_replayed_after_crash() -> None:
    class UnsafeExecutor(_Executor):
        retry_safe_after_ambiguous_failure = False

    journal = InMemoryEffectJournal(lease=timedelta(milliseconds=30))
    executor = UnsafeExecutor()
    registry = EffectExecutorRegistry()
    registry.register(executor)
    service = EffectService(journal, registry)
    command = _command()
    await journal.submit(command)
    abandoned = await journal.claim(command.idempotency_key)
    assert abandoned is not None
    await asyncio.sleep(0.04)
    reclaimed = await journal.claim(command.idempotency_key)
    assert reclaimed is not None and reclaimed.recovered_after_expiry

    result = await service._execute(reclaimed)

    assert result.status is EffectRecordStatus.INDETERMINATE
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_replay_and_purge_do_not_mutate_active_claims() -> None:
    journal = InMemoryEffectJournal()
    command = _command()
    await journal.submit(command)
    first = await journal.claim(command.idempotency_key)
    assert first is not None

    with pytest.raises(ValueError):
        await journal.replay(command.idempotency_key)
    assert await journal.purge_terminal_before(datetime.now(UTC) + timedelta(days=1)) == 0
    assert await journal.get(command.idempotency_key) == first
