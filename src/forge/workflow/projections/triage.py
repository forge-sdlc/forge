"""Project ticket snapshots into triage station requests."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationRequest
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.triage import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    TriageInput,
    TriageKind,
)


def project_triage(
    state: Mapping[str, Any],
    *,
    kind: TriageKind,
    summary: str,
    description: str,
    comments: str,
) -> StationRequest[TriageInput]:
    return StationRequest[TriageInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{kind.value}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=TriageInput(
            kind=kind,
            ticket_key=str(state["ticket_key"]),
            summary=summary,
            description=description,
            comments=comments,
        ),
    )
