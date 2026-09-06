from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from forge.domain import (
    DomainModel,
    EffectCommand,
    ResourceIdentity,
    StationInvocationIdentity,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
    WorkflowIdentity,
)
from forge.workflow.stations.runner import (
    StationDefinition,
    StationRegistry,
    invoke_station,
    run_serialized_async,
)


class Input(DomainModel):
    value: str


class Output(DomainModel):
    value: str


def _request() -> StationRequest[Input]:
    return StationRequest[Input](
        workflow=WorkflowIdentity(run_id="run", workflow_name="test", definition_revision=1),
        invocation=StationInvocationIdentity(invocation_id="inv", station_name="echo"),
        contract_name="echo",
        contract_version="1.0",
        attempt=1,
        requested_at=datetime.now(UTC),
        input=Input(value="hello"),
    )


def _handler(request: StationRequest[Input]) -> StationOutcome[Output]:
    return StationOutcome[Output](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=Output(value=request.input.value),
    )


@pytest.mark.asyncio
async def test_registry_runs_same_serialized_contract_locally() -> None:
    registry = StationRegistry()
    registry.register(StationDefinition("echo", "1.0", Input, _handler))

    result = await run_serialized_async("echo", _request().model_dump_json(), registry=registry)

    assert StationOutcome[Output].model_validate_json(result).output == Output(value="hello")


@pytest.mark.asyncio
async def test_effects_must_complete_before_outcome_is_returned() -> None:
    request = _request()
    effect = EffectCommand(
        effect_id="effect",
        idempotency_key="effect-key",
        workflow=request.workflow,
        operation="test.write",
        target=ResourceIdentity(resource_type="test", external_id="1"),
    )

    def handler(value: StationRequest[Input]) -> StationOutcome[Output]:
        return _handler(value).model_copy(update={"requested_effects": (effect,)})

    service = AsyncMock()
    outcome = await invoke_station(
        StationDefinition("echo", "1.0", Input, handler),
        request,
        effect_service=service,
    )

    service.execute_required.assert_awaited_once_with(effect)
    assert outcome.output == Output(value="hello")


@pytest.mark.asyncio
async def test_effect_emission_fails_closed_without_durable_runtime() -> None:
    request = _request()
    effect = EffectCommand(
        effect_id="effect",
        idempotency_key="effect-key",
        workflow=request.workflow,
        operation="test.write",
        target=ResourceIdentity(resource_type="test", external_id="1"),
    )

    def handler(value: StationRequest[Input]) -> StationOutcome[Output]:
        return _handler(value).model_copy(update={"requested_effects": (effect,)})

    with pytest.raises(ValueError, match="no durable effect service"):
        await invoke_station(StationDefinition("echo", "1.0", Input, handler), request)
