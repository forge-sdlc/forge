"""Construct typed agent-operation station requests."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationRequest
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.agent_operation import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    AgentOperationInput,
)


def project_agent_operation(
    state: Mapping[str, Any],
    operation: AgentOperationInput,
    *,
    discriminator: str,
) -> StationRequest[AgentOperationInput]:
    return StationRequest[AgentOperationInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{discriminator}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        policy_context={"discriminator": discriminator},
        input=operation,
    )
