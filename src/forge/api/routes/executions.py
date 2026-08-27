"""Operator read API for durable workflow execution state."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from forge.config import get_settings
from forge.effects import RedisEffectJournal
from forge.orchestrator.checkpointer import get_checkpointer
from forge.read_models import ExecutionReadModel, TimelinePage, project_execution
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import build_process_manifest

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def require_operator(authorization: Annotated[str | None, Header()] = None) -> None:
    configured = get_settings().forge_operator_token.get_secret_value()
    if not configured:
        raise HTTPException(status_code=503, detail="Operator API token is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="Invalid operator credentials")


async def load_execution_read_model(
    ticket_key: str,
    *,
    checkpointer: Any = None,
    effect_journal: Any = None,
) -> ExecutionReadModel | None:
    saver = checkpointer or await get_checkpointer()
    raw = await saver.aget({"configurable": {"thread_id": ticket_key}})
    if raw is None:
        return None
    checkpoint = raw.get("channel_values", raw)
    definition_value = checkpoint.get("workflow_definition")
    manifest = None
    if isinstance(definition_value, dict):
        definition = load_workflow_value(definition_value)
        if definition.digest != checkpoint.get("workflow_digest"):
            raise ValueError("Pinned workflow definition digest does not match checkpoint")
        manifest = build_process_manifest(definition)
    journal = effect_journal or RedisEffectJournal()
    effects = await journal.list_for_workflow(str(checkpoint.get("thread_id") or ticket_key))
    return project_execution(checkpoint, effects=effects, manifest=manifest)


@router.get("/{ticket_key}/execution", response_model=ExecutionReadModel)
async def get_execution(
    ticket_key: str, _authorized: Annotated[None, Depends(require_operator)]
) -> ExecutionReadModel:
    try:
        model = await load_execution_read_model(ticket_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    return model


@router.get("/{ticket_key}/execution/timeline", response_model=TimelinePage)
async def get_execution_timeline(
    ticket_key: str,
    _authorized: Annotated[None, Depends(require_operator)],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TimelinePage:
    model = await load_execution_read_model(ticket_key)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    end = min(cursor + limit, len(model.timeline))
    return TimelinePage(
        items=model.timeline[cursor:end],
        next_cursor=end if end < len(model.timeline) else None,
        total=len(model.timeline),
    )
