"""Operator read API for durable workflow execution state."""

from __future__ import annotations

import secrets
from time import perf_counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from forge.api.routes.metrics import observe_read_model_latency, record_execution_read_model
from forge.config import get_settings
from forge.effects import RedisEffectJournal
from forge.orchestrator.checkpointer import get_checkpointer
from forge.read_models import (
    ExecutionReadModel,
    RedisExecutionTimelineStore,
    TimelinePage,
    project_execution,
)
from forge.reconciliation import RedisObservationLedger
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
    observation_ledger: Any = None,
    timeline_store: Any = None,
) -> ExecutionReadModel | None:
    # Injected checkpointers are used by tests and migration tooling.  Avoid
    # opening external Redis adapters in those callers while the production
    # route (which supplies no adapter) gets the durable stores by default.
    production_defaults = checkpointer is None
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
    run_id = str(checkpoint.get("thread_id") or ticket_key)
    ledger = observation_ledger
    if ledger is None and production_defaults:
        ledger = RedisObservationLedger()
    decisions = ()
    if ledger is not None:
        history_for_run = getattr(ledger, "history_for_run", None)
        if history_for_run is not None:
            decisions = tuple(await history_for_run(run_id))

    store = timeline_store
    if store is None and production_defaults:
        store = RedisExecutionTimelineStore()
    persisted_timeline = ()
    if store is not None:
        list_records = getattr(store, "list", None)
        if list_records is not None:
            persisted_timeline = tuple(await list_records(run_id))

    return project_execution(
        checkpoint,
        effects=effects,
        manifest=manifest,
        observation_decisions=_deduplicate_observation_decisions(
            [*checkpoint.get("observation_history", ()), *decisions]
        ),
        timeline_entries=persisted_timeline,
    )


def _deduplicate_observation_decisions(decisions: list[Any]) -> tuple[Any, ...]:
    """Merge checkpoint and ledger history without duplicate deliveries."""
    result = []
    seen: set[tuple[str, str | None]] = set()
    for item in decisions:
        if isinstance(item, dict):
            delivery = item.get("delivery_identity") or item.get("observation_id")
            disposition = item.get("disposition") or item.get("status")
        else:
            delivery = getattr(item, "delivery_identity", None)
            observation = getattr(item, "observation", None)
            delivery = delivery or getattr(observation, "observation_id", None)
            disposition = getattr(item, "disposition", None)
            disposition = getattr(disposition, "value", disposition)
        key = (str(delivery or ""), str(disposition) if disposition is not None else None)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


@router.get("/{ticket_key}/execution", response_model=ExecutionReadModel)
async def get_execution(
    ticket_key: str, _authorized: Annotated[None, Depends(require_operator)]
) -> ExecutionReadModel:
    started = perf_counter()
    try:
        model = await load_execution_read_model(ticket_key)
    except ValueError as exc:
        observe_read_model_latency("execution", perf_counter() - started)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    observe_read_model_latency("execution", perf_counter() - started)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    record_execution_read_model(model)
    return model


@router.get("/{ticket_key}/execution/timeline", response_model=TimelinePage)
async def get_execution_timeline(
    ticket_key: str,
    _authorized: Annotated[None, Depends(require_operator)],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TimelinePage:
    started = perf_counter()
    model = await load_execution_read_model(ticket_key)
    observe_read_model_latency("timeline", perf_counter() - started)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    end = min(cursor + limit, len(model.timeline))
    return TimelinePage(
        items=model.timeline[cursor:end],
        next_cursor=end if end < len(model.timeline) else None,
        total=len(model.timeline),
    )
