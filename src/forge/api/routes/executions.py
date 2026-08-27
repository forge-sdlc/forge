"""Operator read API for durable workflow execution state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from forge.effects import RedisEffectJournal
from forge.orchestrator.checkpointer import get_checkpointer
from forge.read_models import ExecutionReadModel, project_execution
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import build_process_manifest

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


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
async def get_execution(ticket_key: str) -> ExecutionReadModel:
    try:
        model = await load_execution_read_model(ticket_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    return model
