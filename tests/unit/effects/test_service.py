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

    first = (await service.run_due())[0]
    second = (await service.run_due())[0]

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

    await service.run_due()
    completed = (await service.run_due())[0]

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
