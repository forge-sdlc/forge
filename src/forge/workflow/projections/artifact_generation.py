"""Project workflow checkpoints into artifact-generation requests."""

from collections.abc import Mapping
from typing import Any

from forge.domain import JsonValue, StationRequest
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.artifact_generation import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ArtifactGenerationInput,
    ArtifactKind,
)


def project_artifact_generation(
    state: Mapping[str, Any],
    *,
    kind: ArtifactKind,
    source_content: str,
    context: dict[str, JsonValue],
    feedback: str | None = None,
) -> StationRequest[ArtifactGenerationInput]:
    return StationRequest[ArtifactGenerationInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{kind.value}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=ArtifactGenerationInput(
            kind=kind,
            source_content=source_content,
            ticket_key=str(state["ticket_key"]),
            context=context,
            feedback=feedback,
        ),
    )
