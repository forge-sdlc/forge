from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.stations.triage import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    TriageInput,
    TriageKind,
    TriageOutput,
    run_triage_station,
)


def _request(kind: TriageKind) -> StationRequest[TriageInput]:
    return StationRequest[TriageInput](
        workflow=WorkflowIdentity(
            run_id="FORGE-1", workflow_name=kind.value, definition_revision=1
        ),
        invocation=StationInvocationIdentity(
            invocation_id=f"FORGE-1:{kind.value}", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=datetime.now(UTC),
        input=TriageInput(
            kind=kind,
            ticket_key="FORGE-1",
            summary="Failure",
            description="It fails",
        ),
    )


@pytest.mark.asyncio
async def test_sufficient_result_is_typed() -> None:
    agent = AsyncMock()
    agent.run_structured_task.return_value = TriageOutput(sufficient=True)
    with patch("forge.workflow.stations.triage.ForgeAgent", return_value=agent):
        outcome = await run_triage_station(_request(TriageKind.BUG))

    assert outcome.output is not None
    assert outcome.output.sufficient is True
    assert outcome.output.missing_fields == ()


@pytest.mark.asyncio
async def test_missing_fields_are_returned_as_typed_output() -> None:
    agent = AsyncMock()
    agent.run_structured_task.return_value = TriageOutput(
        sufficient=False, missing_fields=("steps", "logs")
    )
    with patch("forge.workflow.stations.triage.ForgeAgent", return_value=agent):
        parsed = await run_triage_station(_request(TriageKind.TASK_TAKEOVER))

    assert parsed.output is not None
    assert parsed.output.missing_fields == ("steps", "logs")
