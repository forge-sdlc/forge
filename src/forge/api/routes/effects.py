"""Authenticated operational API for durable external effects."""

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from forge.config import get_settings
from forge.domain import stable_identity
from forge.effects import EffectRecord, EffectService, create_default_effect_service
from forge.read_models import RedisExecutionTimelineStore, TimelineEntry

router = APIRouter(prefix="/api/v1/effects", tags=["effects"])


def get_effect_service() -> EffectService:
    return create_default_effect_service()


def authorize_operator(authorization: str | None) -> None:
    configured = get_settings().effect_operator_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Effect operator API is disabled",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        supplied, configured.get_secret_value()
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


OperatorAuth = Annotated[str | None, Header(alias="Authorization")]
EffectServiceDep = Annotated[EffectService, Depends(get_effect_service)]


def get_timeline_store() -> RedisExecutionTimelineStore:
    """Build the durable operator timeline adapter for mutation auditing."""
    return RedisExecutionTimelineStore()


TimelineStoreDep = Annotated[RedisExecutionTimelineStore, Depends(get_timeline_store)]


class EffectReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["applied", "not_applied"]
    note: str = Field(min_length=1, max_length=2000)
    provider_reference: str | None = None


@router.get("/workflow/{run_id}", response_model=list[EffectRecord])
async def list_workflow_effects(
    run_id: str, service: EffectServiceDep, authorization: OperatorAuth = None
) -> Sequence[EffectRecord]:
    authorize_operator(authorization)
    return await service.journal.list_for_workflow(run_id)


@router.get("/{idempotency_key}", response_model=EffectRecord)
async def get_effect(
    idempotency_key: str, service: EffectServiceDep, authorization: OperatorAuth = None
) -> EffectRecord:
    authorize_operator(authorization)
    record = await service.journal.get(idempotency_key)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Effect not found")
    return record


@router.post("/{idempotency_key}/replay", response_model=EffectRecord)
async def replay_effect(
    idempotency_key: str,
    service: EffectServiceDep,
    timeline_store: TimelineStoreDep,
    authorization: OperatorAuth = None,
) -> EffectRecord:
    authorize_operator(authorization)
    try:
        replayed = await service.replay(idempotency_key)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Effect not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # The timeline write happens only after the effect journal has accepted the
    # replay mutation.  Unauthorized, missing, and rejected replays therefore
    # cannot manufacture operator-action evidence.
    await timeline_store.append(
        replayed.command.workflow.run_id,
        TimelineEntry(
            event_id=stable_identity(
                "operator-action",
                {
                    "run_id": replayed.command.workflow.run_id,
                    "action": "effect-replay",
                    "effect_id": replayed.command.effect_id,
                    "replay_count": replayed.replay_count,
                },
            ),
            kind="operator_action",
            occurred_at=replayed.updated_at or datetime.now(UTC),
            status="accepted",
            summary="Effect replay accepted",
            details={
                "action": "effect-replay",
                "effect_id": replayed.command.effect_id,
                "idempotency_key": replayed.command.idempotency_key,
                "operation": replayed.command.operation,
                "target": replayed.command.target.external_id,
                "result": replayed.status.value,
                "result_status": replayed.status.value,
                "replay_count": replayed.replay_count,
            },
        ),
    )
    return replayed


@router.post("/{idempotency_key}/reconcile", response_model=EffectRecord)
async def reconcile_effect(
    idempotency_key: str,
    request: EffectReconciliationRequest,
    service: EffectServiceDep,
    timeline_store: TimelineStoreDep,
    authorization: OperatorAuth = None,
) -> EffectRecord:
    """Record an operator's provider-side determination for an ambiguous attempt."""
    authorize_operator(authorization)
    try:
        reconciled = await service.reconcile(
            idempotency_key,
            applied=request.decision == "applied",
            note=request.note,
            provider_reference=request.provider_reference,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Effect not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await timeline_store.append(
        reconciled.command.workflow.run_id,
        TimelineEntry(
            event_id=stable_identity(
                "operator-action",
                {
                    "run_id": reconciled.command.workflow.run_id,
                    "action": "effect-reconcile",
                    "effect_id": reconciled.command.effect_id,
                    "reconciliation_count": len(reconciled.reconciliation_history),
                },
            ),
            kind="operator_action",
            occurred_at=reconciled.updated_at,
            status="accepted",
            summary="Effect outcome reconciled",
            details={
                "action": "effect-reconcile",
                "effect_id": reconciled.command.effect_id,
                "idempotency_key": reconciled.command.idempotency_key,
                "decision": request.decision,
                "note": request.note,
                "provider_reference": request.provider_reference,
                "result_status": reconciled.status.value,
            },
        ),
    )
    return reconciled
