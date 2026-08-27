"""Pure projection from durable execution records to an operator view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from forge.domain import Observation
from forge.effects import EffectRecord
from forge.read_models.models import (
    DefinitionView,
    EffectView,
    ExecutionReadModel,
    ExecutionStatus,
    MigrationView,
    NextTransitionView,
    ObservationView,
    StationAttemptView,
    TimelineEntry,
    WaitingView,
)
from forge.workflow.declarative.manifest import ProcessChangeImpact, ProcessManifest


def project_execution(
    checkpoint: Mapping[str, Any],
    *,
    effects: Sequence[EffectRecord] = (),
    manifest: ProcessManifest | None = None,
    last_observation: Observation | None = None,
    migration: ProcessChangeImpact | None = None,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=1),
) -> ExecutionReadModel:
    """Build a read-only explanation without consulting Jira labels or logs."""
    now = now or datetime.now(UTC)
    ticket_key = str(checkpoint.get("ticket_key") or checkpoint.get("thread_id") or "unknown")
    run_id = str(checkpoint.get("thread_id") or ticket_key)
    position = str(checkpoint.get("current_node") or "entry")
    status = _status(checkpoint, position)
    waiting = _waiting(checkpoint, status)
    permitted = _permitted_commands(status, position)
    transitions = tuple(
        NextTransitionView(outcome=item.outcome, target=item.target)
        for item in (manifest.transitions if manifest else ())
        if item.source == position
    )
    definition = DefinitionView(
        name=str(checkpoint.get("workflow_name") or checkpoint.get("ticket_type") or "legacy"),
        revision=int(checkpoint.get("workflow_revision") or 1),
        digest=checkpoint.get("workflow_digest"),
        available=manifest is not None,
        manifest=manifest.model_dump(mode="json") if manifest else None,
    )
    observation_view = _observation(last_observation, checkpoint, now, stale_after)
    return ExecutionReadModel(
        run_id=run_id,
        ticket_key=ticket_key,
        status=status,
        current_position=position,
        definition=definition,
        permitted_commands=permitted,
        next_transitions=transitions,
        waiting=waiting,
        last_observation=observation_view,
        station_attempts=_station_attempts(checkpoint),
        effects=tuple(_effect(record) for record in effects),
        migration=MigrationView(
            eligible=migration.compatible_for_in_flight if migration else None,
            incompatibilities=(
                (*migration.missing_resume_mappings, *migration.notes) if migration else ()
            ),
        ),
        timeline=_timeline(checkpoint, effects),
    )


def _status(checkpoint: Mapping[str, Any], position: str) -> ExecutionStatus:
    if position in {"complete", "__end__"}:
        return ExecutionStatus.COMPLETED
    if checkpoint.get("is_blocked"):
        return ExecutionStatus.BLOCKED
    if checkpoint.get("last_error"):
        return ExecutionStatus.FAILED
    if checkpoint.get("is_paused"):
        return ExecutionStatus.WAITING
    return ExecutionStatus.RUNNING


def _waiting(checkpoint: Mapping[str, Any], status: ExecutionStatus) -> WaitingView | None:
    updated_at = _datetime(checkpoint.get("updated_at"))
    if status is ExecutionStatus.BLOCKED:
        return WaitingView(
            code="blocked",
            message=str(checkpoint.get("last_error") or "Workflow requires operator intervention"),
            since=updated_at,
            recovery="Resolve the blocking condition, then issue retry or cancel.",
        )
    if status is ExecutionStatus.FAILED:
        return WaitingView(
            code="failed",
            message=str(checkpoint.get("last_error")),
            since=updated_at,
            recovery="Inspect the failed station/effect and issue retry or cancel.",
        )
    if status is ExecutionStatus.WAITING:
        return WaitingView(
            code="gate",
            message=f"Waiting at {checkpoint.get('current_node') or 'an approval gate'}",
            since=updated_at,
            recovery="Provide an eligible approval, rejection, question, retry, or cancel command.",
        )
    return None


def _permitted_commands(status: ExecutionStatus, position: str) -> tuple[str, ...]:
    if status is ExecutionStatus.COMPLETED:
        return ()
    if status in {ExecutionStatus.BLOCKED, ExecutionStatus.FAILED}:
        return ("retry", "cancel")
    if status is ExecutionStatus.WAITING:
        commands = ["resume", "retry", "cancel"]
        if position.endswith("_gate"):
            commands[0:0] = ["approve", "reject"]
        return tuple(commands)
    return ("synchronize", "cancel")


def _observation(
    observation: Observation | None,
    checkpoint: Mapping[str, Any],
    now: datetime,
    stale_after: timedelta,
) -> ObservationView:
    if observation is None:
        return ObservationView(
            available=False,
            conflicting=bool(checkpoint.get("external_state_conflict")),
        )
    observed_at = observation.observed_at
    comparable_now = now if now.tzinfo else now.replace(tzinfo=UTC)
    comparable_observed = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
    return ObservationView(
        observation_id=observation.observation_id,
        source_system=observation.source_system,
        observed_at=observation.observed_at,
        stale=comparable_now - comparable_observed > stale_after,
        conflicting=bool(checkpoint.get("external_state_conflict")),
        available=True,
    )


def _station_attempts(checkpoint: Mapping[str, Any]) -> tuple[StationAttemptView, ...]:
    return tuple(
        StationAttemptView(
            station_name=str(item.get("station_name") or "unknown"),
            invocation_id=str(item.get("invocation_id") or "unknown"),
            attempt=int(item.get("attempt") or 1),
            status=str(item.get("status") or "unknown"),
            completed_at=_datetime(item.get("completed_at")),
            reason=item.get("reason"),
        )
        for item in checkpoint.get("station_history") or []
    )


def _effect(record: EffectRecord) -> EffectView:
    result = record.result
    return EffectView(
        effect_id=record.command.effect_id,
        operation=record.command.operation,
        target=record.command.target.external_id,
        status=record.status.value,
        attempt=record.attempt,
        updated_at=record.updated_at,
        provider_reference=result.provider_reference if result else None,
        error=result.error_message if result else None,
    )


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def _timeline(
    checkpoint: Mapping[str, Any], effects: Sequence[EffectRecord]
) -> tuple[TimelineEntry, ...]:
    entries: list[TimelineEntry] = []
    for item in checkpoint.get("command_decisions") or []:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("decision_id") or item.get("command_id") or "command"),
                kind="command_decision",
                occurred_at=_datetime(item.get("decided_at")),
                status=item.get("status"),
                summary=str(item.get("reason") or "Command evaluated"),
                details={
                    key: value
                    for key, value in {
                        "command_id": item.get("command_id"),
                        "command_type": item.get("command_type"),
                        "observation_id": item.get("observation_id"),
                    }.items()
                    if value is not None
                },
            )
        )
    for item in checkpoint.get("transition_history") or []:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("transition_id") or "transition"),
                kind="transition",
                occurred_at=_datetime(item.get("occurred_at")),
                status="committed",
                summary=f"{item.get('source', 'unknown')} → {item.get('target', 'unknown')}",
                details={"source": str(item.get("source")), "target": str(item.get("target"))},
            )
        )
    for item in checkpoint.get("station_history") or []:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("invocation_id") or "station"),
                kind="station_attempt",
                occurred_at=_datetime(item.get("completed_at")),
                status=str(item.get("status") or "unknown"),
                summary=f"Station {item.get('station_name', 'unknown')} attempt {item.get('attempt', 1)}",
                details={"reason": str(item["reason"])} if item.get("reason") else {},
            )
        )
    for record in effects:
        entries.append(
            TimelineEntry(
                event_id=record.command.effect_id,
                kind="effect",
                occurred_at=record.updated_at,
                status=record.status.value,
                summary=f"{record.command.operation} on {record.command.target.external_id}",
                details={
                    "attempt": record.attempt,
                    "idempotency_key": record.command.idempotency_key,
                    "replay_count": record.replay_count,
                },
            )
        )
    entries.sort(
        key=lambda entry: (
            entry.occurred_at or datetime.min.replace(tzinfo=UTC),
            entry.kind,
            entry.event_id,
        )
    )
    return tuple(entries)
