from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.stations.artifact_generation import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ArtifactGenerationInput,
    ArtifactKind,
    run_artifact_generation_station,
)


def _request(kind: ArtifactKind, *, feedback: str | None = None):
    now = datetime.now(UTC)
    return StationRequest[ArtifactGenerationInput](
        workflow=WorkflowIdentity(run_id="FORGE-1", workflow_name="feature", definition_revision=1),
        invocation=StationInvocationIdentity(
            invocation_id=f"FORGE-1:{kind}", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=now,
        input=ArtifactGenerationInput(
            kind=kind,
            source_content="source",
            ticket_key="FORGE-1",
            context={"summary": "Feature"},
            feedback=feedback,
        ),
    )


@pytest.mark.asyncio
async def test_prd_generation_uses_only_projected_input() -> None:
    agent = AsyncMock()
    agent.generate_prd.return_value = "generated PRD"
    with patch("forge.workflow.stations.artifact_generation.ForgeAgent", return_value=agent):
        outcome = await run_artifact_generation_station(_request(ArtifactKind.PRD))

    agent.generate_prd.assert_awaited_once_with("source", {"summary": "Feature"})
    assert outcome.output is not None
    assert outcome.output.content == "generated PRD"
    agent.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_revision_is_a_station_operation() -> None:
    agent = AsyncMock()
    agent.regenerate_with_feedback.return_value = "revised spec"
    with patch("forge.workflow.stations.artifact_generation.ForgeAgent", return_value=agent):
        outcome = await run_artifact_generation_station(
            _request(ArtifactKind.SPEC, feedback="clarify behavior")
        )

    agent.regenerate_with_feedback.assert_awaited_once()
    assert outcome.output is not None
    assert outcome.output.content == "revised spec"
