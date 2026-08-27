"""Pure station that turns approved provider mutations into durable effect intents."""

from __future__ import annotations

from pydantic import Field

from forge.domain import (
    DomainModel,
    EffectCommand,
    JsonValue,
    ResourceIdentity,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
    stable_identity,
)

CONTRACT_NAME = "persistence-actions"
CONTRACT_VERSION = "1.0"


class PersistenceAction(DomainModel):
    operation: str
    resource_type: str
    external_id: str
    namespace: str | None = None
    logical_action: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    expected_precondition: dict[str, JsonValue] = Field(default_factory=dict)


class PersistenceInput(DomainModel):
    actions: tuple[PersistenceAction, ...]


class PersistenceOutput(DomainModel):
    effect_ids: tuple[str, ...]


def run_persistence_station(
    request: StationRequest[PersistenceInput],
) -> StationOutcome[PersistenceOutput]:
    effects: list[EffectCommand] = []
    for action in request.input.actions:
        effect_id = stable_identity(
            "effect",
            {
                "run_id": request.workflow.run_id,
                "operation": action.operation,
                "resource_type": action.resource_type,
                "external_id": action.external_id,
                "namespace": action.namespace or "",
                "logical_action": action.logical_action,
            },
        )
        effects.append(
            EffectCommand(
                effect_id=effect_id,
                idempotency_key=effect_id,
                workflow=request.workflow,
                operation=action.operation,
                target=ResourceIdentity(
                    resource_type=action.resource_type,
                    external_id=action.external_id,
                    namespace=action.namespace,
                ),
                expected_precondition=action.expected_precondition,
                payload=action.payload,
            )
        )
    return StationOutcome[PersistenceOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=PersistenceOutput(effect_ids=tuple(item.effect_id for item in effects)),
        requested_effects=tuple(effects),
    )
