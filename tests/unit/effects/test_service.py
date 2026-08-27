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
)


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
