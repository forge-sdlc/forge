"""Control-plane adapter for executing typed persistence station actions."""

from collections.abc import Mapping, Sequence
from typing import Any

from forge.domain import StationRequest
from forge.effects import EffectRecord, EffectService, create_default_effect_service
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.persistence import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    PersistenceAction,
    PersistenceInput,
)
from forge.workflow.stations.runner import invoke_builtin_station


async def execute_persistence_actions(
    state: Mapping[str, Any],
    actions: Sequence[PersistenceAction],
    *,
    discriminator: str,
    effect_service: EffectService | None = None,
) -> tuple[EffectRecord, ...]:
    request = StationRequest[PersistenceInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{discriminator}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=PersistenceInput(actions=tuple(actions)),
    )
    service = effect_service or create_default_effect_service()
    records: list[EffectRecord] = []
    await invoke_builtin_station(request, effect_service=service, effect_records=records)
    return tuple(records)
