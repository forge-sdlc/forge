"""Typed station for PRD and specification content generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from forge.domain import (
    DomainModel,
    JsonValue,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
)
from forge.integrations.agents import ForgeAgent

CONTRACT_NAME = "artifact-generation"
CONTRACT_VERSION = "1.0"


class ArtifactKind(StrEnum):
    PRD = "prd"
    SPEC = "spec"
    EPICS = "epics"
    TASK = "task"


class ArtifactGenerationInput(DomainModel):
    kind: ArtifactKind
    source_content: str
    ticket_key: str
    context: dict[str, JsonValue] = Field(default_factory=dict)
    feedback: str | None = None


class ArtifactGenerationOutput(DomainModel):
    kind: ArtifactKind
    content: JsonValue


async def run_artifact_generation_station(
    request: StationRequest[ArtifactGenerationInput],
) -> StationOutcome[ArtifactGenerationOutput]:
    """Generate content without reading workflow state or provider resources."""
    value = request.input
    agent = ForgeAgent()
    try:
        if value.feedback:
            content = await agent.regenerate_with_feedback(
                original_content=value.source_content,
                feedback=value.feedback,
                content_type=value.kind.value,
                ticket_key=value.ticket_key,
                context=dict(value.context),
            )
        elif value.kind is ArtifactKind.PRD:
            content = await agent.generate_prd(value.source_content, dict(value.context))
        elif value.kind is ArtifactKind.SPEC:
            content = await agent.generate_spec(value.source_content, dict(value.context))
        elif value.kind is ArtifactKind.EPICS:
            content = await agent.generate_epics(value.source_content, dict(value.context))
        else:
            raise ValueError("Task generation requires revision feedback")
    finally:
        await agent.close()
    return StationOutcome[ArtifactGenerationOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=ArtifactGenerationOutput(kind=value.kind, content=content),
    )
