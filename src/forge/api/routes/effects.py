"""Authenticated operational API for durable external effects."""

import secrets
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from forge.config import get_settings
from forge.effects import EffectRecord, EffectService, create_default_effect_service

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
    idempotency_key: str, service: EffectServiceDep, authorization: OperatorAuth = None
) -> EffectRecord:
    authorize_operator(authorization)
    try:
        return await service.replay(idempotency_key)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Effect not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
