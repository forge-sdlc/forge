"""Pure projection from durable execution records to an operator view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from forge.domain import Observation
from forge.effects import EffectRecord
from forge.read_models.models import (
    DefinitionView,
    EffectAttemptView,
    EffectView,
    ExecutionReadModel,
    ExecutionStatus,
    MigrationView,
    NextTransitionView,
    ObservationView,
    RecoveryOptionView,
    RuleClauseView,
    RuleExplanationView,
    StationAttemptView,
    TimelineEntry,
    WaitingView,
)
from forge.workflow.declarative.manifest import ProcessChangeImpact, ProcessManifest
from forge.workflow.preconditions import has_capability


def project_execution(
    checkpoint: Mapping[str, Any],
    *,
    effects: Sequence[EffectRecord] = (),
    manifest: ProcessManifest | None = None,
    last_observation: Observation | None = None,
    observation_decisions: Sequence[Any] = (),
    migration: ProcessChangeImpact | None = None,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=1),
    migrations: Sequence[Mapping[str, Any]] = (),
    operator_actions: Sequence[Mapping[str, Any]] = (),
    timeline_entries: Sequence[TimelineEntry] = (),
) -> ExecutionReadModel:
    """Build a read-only explanation without consulting Jira labels or logs."""
    now = now or datetime.now(UTC)
    ticket_key = str(checkpoint.get("ticket_key") or checkpoint.get("thread_id") or "unknown")
    run_id = str(checkpoint.get("thread_id") or ticket_key)
    position = str(checkpoint.get("current_node") or "entry")
    status = _status(checkpoint, position)
    waiting = _waiting(checkpoint, status)
    permitted = _permitted_commands(status, position, checkpoint, manifest)
    transitions = tuple(
        NextTransitionView(outcome=item.outcome, target=item.target)
        for item in (manifest.transitions if manifest else ())
        if item.source == position
    )
    definition = DefinitionView(
        name=str(checkpoint.get("workflow_name") or checkpoint.get("ticket_type") or "legacy"),
        revision=_definition_revision(checkpoint),
        digest=_definition_digest(checkpoint),
        available=manifest is not None or isinstance(checkpoint.get("workflow_definition"), dict),
        # A pinned canonical artifact is the authoritative definition.  The
        # compiled manifest remains useful as a fallback for callers that only
        # have inspection data (and for legacy checkpoints).
        manifest=(
            {
                **checkpoint["workflow_definition"],
                # Canonical workflow artifacts intentionally do not require a
                # derived digest field; retain it in the view for clients
                # that consumed the original manifest-shaped response.
                **({"digest": manifest.digest} if manifest else {}),
            }
            if isinstance(checkpoint.get("workflow_definition"), dict)
            else manifest.model_dump(mode="json") if manifest else None
        ),
    )
    decisions = tuple(observation_decisions) or _checkpoint_observation_decisions(checkpoint)
    observation_view = _observation(last_observation, checkpoint, now, stale_after, decisions)
    stale_inputs, conflicting_inputs = _input_views(decisions, now, stale_after)
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
        stale_observations=stale_inputs,
        conflicting_observations=conflicting_inputs,
        station_attempts=_station_attempts(checkpoint),
        effects=tuple(_effect(record) for record in effects),
        recovery_options=_recovery_options(waiting, permitted),
        explanations=_rule_explanations(checkpoint, position, manifest),
        migration=MigrationView(
            eligible=migration.compatible_for_in_flight if migration else None,
            incompatibilities=(
                (*migration.missing_resume_mappings, *migration.notes) if migration else ()
            ),
        ),
        timeline=_timeline(
            checkpoint,
            effects,
            decisions,
            migrations=migrations,
            operator_actions=operator_actions,
            timeline_entries=timeline_entries,
        ),
    )


def _definition_revision(checkpoint: Mapping[str, Any]) -> int:
    value = checkpoint.get("workflow_definition_revision", checkpoint.get("workflow_revision"))
    if value is None:
        definition = checkpoint.get("workflow_definition")
        if isinstance(definition, Mapping):
            metadata = definition.get("metadata")
            if isinstance(metadata, Mapping):
                value = metadata.get("revision")
    value = value or 1
    try:
        return int(value or 1)
    except (TypeError, ValueError):
        return 1


def _definition_digest(checkpoint: Mapping[str, Any]) -> str | None:
    value = checkpoint.get("workflow_definition_digest", checkpoint.get("workflow_digest"))
    # Canonical definitions do not carry their digest; callers that have a
    # compiled manifest still supply it separately.  Never hash a possibly
    # non-canonical mapping on the read side.
    return str(value) if value else None


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
            code=str(checkpoint.get("wait_code") or checkpoint.get("block_code") or "blocked"),
            message=str(
                checkpoint.get("blocking_reason")
                or checkpoint.get("last_error")
                or "Workflow requires operator intervention"
            ),
            since=updated_at,
            recovery=str(
                checkpoint.get("recovery_reason")
                or "Resolve the blocking condition, then issue retry or cancel."
            ),
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
            code=str(checkpoint.get("wait_code") or "gate"),
            message=str(
                checkpoint.get("waiting_reason")
                or checkpoint.get("wait_reason")
                or f"Waiting at {checkpoint.get('current_node') or 'an approval gate'}"
            ),
            since=updated_at,
            recovery=str(
                checkpoint.get("recovery_reason")
                or "Provide an eligible approval, rejection, question, retry, or cancel command."
            ),
        )
    return None


def _permitted_commands(
    status: ExecutionStatus,
    position: str,
    checkpoint: Mapping[str, Any],
    manifest: ProcessManifest | None,
) -> tuple[str, ...]:
    # A persisted decision is authoritative when available.  This keeps this
    # read side from silently inventing commands for an unfamiliar workflow.
    explicit = checkpoint.get("permitted_commands")
    if isinstance(explicit, (list, tuple)):
        return tuple(str(command) for command in explicit)
    if status is ExecutionStatus.COMPLETED:
        return ()
    if status in {ExecutionStatus.BLOCKED, ExecutionStatus.FAILED}:
        return ("retry", "cancel")
    if status is ExecutionStatus.WAITING:
        commands = ["resume", "retry", "cancel"]
        node = next((item for item in (manifest.nodes if manifest else ()) if item.name == position), None)
        # Gate-ness comes from the pinned process manifest, never from a name
        # convention such as ``*_gate``.
        if node is not None and node.kind.value == "gate":
            commands[0:0] = ["approve", "reject"]
        return tuple(commands)
    return ("synchronize", "cancel")


def _observation(
    observation: Observation | None,
    checkpoint: Mapping[str, Any],
    now: datetime,
    stale_after: timedelta,
    decisions: Sequence[Any] = (),
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
        stale=_observation_is_stale(observation, decisions, comparable_now, comparable_observed, stale_after),
        conflicting=bool(checkpoint.get("external_state_conflict"))
        or _observation_has_disposition(observation, decisions, "conflict"),
        available=True,
        disposition="accepted",
        resource_revision=observation.resource_revision,
        revision_order=observation.revision_order,
    )


def _decision_disposition(item: Any) -> str | None:
    value = (
        item.get("disposition")
        if isinstance(item, Mapping)
        else getattr(item, "disposition", None)
    )
    return getattr(value, "value", value)


def _checkpoint_observation_decisions(checkpoint: Mapping[str, Any]) -> tuple[Any, ...]:
    value = checkpoint.get("observation_history") or checkpoint.get("observations") or ()
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _decision_observation(item: Any) -> Any:
    observation = (
        item.get("observation")
        if isinstance(item, Mapping)
        else getattr(item, "observation", None)
    )
    if isinstance(observation, Mapping):
        return _MappingObservation(observation)
    return observation


class _MappingObservation:
    """Small adapter for JSON checkpoints containing flattened observations."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        self._value = value
        self.observation_id = str(value.get("observation_id") or "observation")
        self.source_system = value.get("source_system")
        self.source = value.get("source", "unknown")
        self.resource_revision = value.get("resource_revision")
        self.revision_order = value.get("revision_order")
        self.observed_at = _datetime(value.get("observed_at")) or datetime.min.replace(tzinfo=UTC)


def _observation_id(observation: Any) -> str | None:
    value = getattr(observation, "observation_id", None)
    return str(value) if value else None


def _decision_observation_id(item: Any) -> str | None:
    observation = _decision_observation(item)
    identity = _observation_id(observation)
    if identity:
        return identity
    value = item.get("observation_id") if isinstance(item, Mapping) else None
    return str(value) if value else None


def _decision_delivery_identity(item: Any) -> str | None:
    value = item.get("delivery_identity") if isinstance(item, Mapping) else getattr(item, "delivery_identity", None)
    return str(value) if value else None


def _observation_has_disposition(
    observation: Any, decisions: Sequence[Any], disposition: str
) -> bool:
    identity = _observation_id(observation)
    return any(
        _decision_disposition(item) == disposition
        and (_decision_observation_id(item) in {None, identity})
        for item in decisions
    )


def _observation_is_stale(
    observation: Any,
    decisions: Sequence[Any],
    comparable_now: datetime,
    comparable_observed: datetime,
    stale_after: timedelta,
) -> bool:
    return _observation_has_disposition(observation, decisions, "stale") or (
        comparable_now - comparable_observed > stale_after
    )


def _input_view(item: Any, now: datetime, stale_after: timedelta) -> ObservationView:
    observation = _decision_observation(item)
    disposition = _decision_disposition(item)
    reason = item.get("reason") if isinstance(item, Mapping) else getattr(item, "reason", None)
    if observation is not None:
        observed_at = observation.observed_at
        current_now = now if now.tzinfo else now.replace(tzinfo=UTC)
        current_observed = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
        return ObservationView(
            observation_id=observation.observation_id,
            source_system=str(observation.source_system) if observation.source_system else None,
            observed_at=observed_at,
            stale=disposition == "stale" or current_now - current_observed > stale_after,
            conflicting=disposition == "conflict",
            available=True,
            disposition=disposition,
            reason=reason,
            resource_revision=observation.resource_revision,
            revision_order=observation.revision_order,
        )
    # Checkpoint JSON may contain a flattened decision record.  Keep the
    # record visible even when older checkpoints cannot hydrate Observation.
    return ObservationView(
        observation_id=(
            str(item.get("observation_id"))
            if isinstance(item, Mapping) and item.get("observation_id")
            else None
        ),
        source_system=(
            str(item.get("source_system"))
            if isinstance(item, Mapping) and item.get("source_system")
            else None
        ),
        stale=disposition == "stale",
        conflicting=disposition == "conflict",
        available=False,
        disposition=disposition,
        reason=reason,
    )


def _input_views(
    decisions: Sequence[Any], now: datetime, stale_after: timedelta
) -> tuple[tuple[ObservationView, ...], tuple[ObservationView, ...]]:
    stale: list[ObservationView] = []
    conflicting: list[ObservationView] = []
    for item in decisions:
        disposition = _decision_disposition(item)
        view = _input_view(item, now, stale_after)
        if disposition == "stale":
            stale.append(view)
        elif disposition == "conflict":
            conflicting.append(view)
    return tuple(stale), tuple(conflicting)


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
    attempts = [
        EffectAttemptView(
            status=attempt.status.value,
            completed_at=attempt.completed_at,
            provider_reference=attempt.provider_reference,
            error=attempt.error_message,
        )
        for attempt in record.attempt_history
    ]
    # The journal stores prior outcomes in attempt_history and the latest
    # outcome separately.  Expose both so an operator can account for every
    # provider call, including a successful final retry.
    if result is not None and (
        not attempts
        or attempts[-1].completed_at != result.completed_at
        or attempts[-1].status != result.status.value
    ):
        attempts.append(
            EffectAttemptView(
                status=result.status.value,
                completed_at=result.completed_at,
                provider_reference=result.provider_reference,
                error=result.error_message,
            )
        )
    return EffectView(
        effect_id=record.command.effect_id,
        operation=record.command.operation,
        target=record.command.target.external_id,
        status=record.status.value,
        attempt=record.attempt,
        updated_at=record.updated_at,
        provider_reference=result.provider_reference if result else None,
        error=result.error_message if result else None,
        attempts=tuple(attempts),
    )


def _recovery_options(
    waiting: WaitingView | None,
    permitted: Sequence[str],
) -> tuple[RecoveryOptionView, ...]:
    descriptions = {
        "approve": "Provide the approval required by the current gate.",
        "reject": "Reject the current gate and follow its configured branch.",
        "resume": "Resume execution from the persisted checkpoint.",
        "synchronize": "Reconcile the latest external observations.",
        "retry": "Retry the failed or blocked operation from its durable boundary.",
        "cancel": "Cancel the execution without changing external state.",
    }
    return tuple(
        RecoveryOptionView(
            command=command,
            description=(waiting.recovery if command == "retry" and waiting and waiting.recovery else descriptions.get(command, "Issue this permitted command.")),
        )
        for command in permitted
    )


def _rule_explanations(
    checkpoint: Mapping[str, Any], position: str, manifest: ProcessManifest | None
) -> tuple[RuleExplanationView, ...]:
    """Project evaluated contract clauses, including clauses that are true.

    The durable precondition result/history is retained as evidence, while the
    current clause values are evaluated against the checkpoint's explicit
    capabilities (or the compatibility predicates for legacy state).
    """
    profile_name = checkpoint.get("workflow_state_profile") or (
        manifest.state_profile if manifest else None
    )
    contract = None
    if profile_name:
        try:
            from forge.workflow.declarative.catalog import get_state_profile

            contract = get_state_profile(str(profile_name)).contracts.get(position)
        except (KeyError, ValueError):
            contract = None
    persisted = checkpoint.get("precondition_result")
    if contract is None and not isinstance(persisted, Mapping):
        return ()

    clauses: list[RuleClauseView] = []
    if contract is not None:
        for requirement in contract.requires:
            capability = (
                requirement.capability.value
                if hasattr(requirement.capability, "value")
                else str(requirement.capability)
            )
            clauses.append(
                RuleClauseView(
                    capability=capability,
                    satisfied=has_capability(checkpoint, requirement.capability),
                    on_missing=requirement.on_missing.value,
                    reason=requirement.reason,
                )
            )
    # For custom contracts, retain false clauses recorded by the runtime even
    # though this process cannot import an arbitrary project predicate.
    if not clauses and isinstance(persisted, Mapping):
        missing = persisted.get("missing") or ()
        missing_names = {str(value) for value in missing}
        for name in sorted(missing_names):
            clauses.append(
                RuleClauseView(
                    capability=name,
                    satisfied=False,
                    on_missing=str(persisted.get("action")) if persisted.get("action") else None,
                    reason=str(persisted.get("reason")) if persisted.get("reason") else None,
                )
            )
    action = persisted.get("action") if isinstance(persisted, Mapping) else None
    satisfied = all(clause.satisfied for clause in clauses) if clauses else action in {None, "proceed"}
    summary = (
        "All required workflow rules are satisfied."
        if satisfied
        else str(persisted.get("reason")) if isinstance(persisted, Mapping) and persisted.get("reason")
        else "One or more required workflow rules are false."
    )
    # A checkpoint can have several evaluations over time.  The current
    # result is the primary explanation; history is represented in timeline.
    return (
        RuleExplanationView(
            rule="node_preconditions",
            node=position,
            satisfied=satisfied,
            action=str(action) if action else None,
            summary=summary,
            clauses=tuple(clauses),
        ),
    )


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def _timeline(
    checkpoint: Mapping[str, Any],
    effects: Sequence[EffectRecord],
    decisions: Sequence[Any] = (),
    *,
    migrations: Sequence[Mapping[str, Any]] = (),
    operator_actions: Sequence[Mapping[str, Any]] = (),
    timeline_entries: Sequence[TimelineEntry] = (),
) -> tuple[TimelineEntry, ...]:
    """Aggregate all durable execution records into a stable event stream."""
    entries: list[TimelineEntry] = list(timeline_entries)
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
    for item in decisions:
        observation = _decision_observation(item)
        if observation is not None:
            occurred_at = observation.observed_at
            event_id = observation.observation_id
            details = {
                "source": getattr(observation.source, "value", observation.source),
                "source_system": observation.source_system,
                "resource_revision": observation.resource_revision,
                "revision_order": observation.revision_order,
            }
        else:
            occurred_at = _datetime(item.get("decided_at")) if isinstance(item, Mapping) else getattr(item, "decided_at", None)
            event_id = str(item.get("observation_id") or "observation") if isinstance(item, Mapping) else "observation"
            details = {}
        disposition = _decision_disposition(item)
        delivery_identity = _decision_delivery_identity(item)
        if delivery_identity or disposition:
            # One provider revision may legitimately have several durable
            # decisions (accepted, duplicate, stale, or conflict).  Include
            # decision identity so projection does not collapse that audit
            # history into one observation event.
            event_id = ":".join(
                part for part in (event_id, delivery_identity, disposition) if part
            )
        reason = item.get("reason") if isinstance(item, Mapping) else getattr(item, "reason", None)
        entries.append(
            TimelineEntry(
                event_id=event_id,
                kind="observation",
                occurred_at=occurred_at,
                status=disposition,
                summary=str(reason or "External observation evaluated"),
                details={key: value for key, value in details.items() if value is not None},
            )
        )
    for item in checkpoint.get("precondition_history") or []:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("event_id") or item.get("node") or "precondition"),
                kind="rule_evaluation",
                occurred_at=_datetime(item.get("occurred_at") or item.get("evaluated_at")),
                status=str(item.get("action") or "evaluated"),
                summary=str(item.get("reason") or "Workflow rule evaluated"),
                details={
                    key: value
                    for key, value in item.items()
                    if key not in {"event_id", "node", "occurred_at", "evaluated_at", "action", "reason"}
                },
            )
        )
    for item in [*(checkpoint.get("migration_history") or []), *migrations]:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("migration_id") or item.get("event_id") or "migration"),
                kind="migration",
                occurred_at=_datetime(item.get("occurred_at") or item.get("updated_at")),
                status=str(item.get("status") or item.get("classification") or "recorded"),
                summary=str(item.get("reason") or "Workflow definition migration evaluated"),
                details={
                    key: value
                    for key, value in item.items()
                    if key not in {"reason", "occurred_at", "updated_at"}
                },
            )
        )
    for item in [
        *(checkpoint.get("operator_actions") or []),
        *(checkpoint.get("operator_history") or []),
        *operator_actions,
    ]:
        entries.append(
            TimelineEntry(
                event_id=str(item.get("action_id") or item.get("event_id") or "operator-action"),
                kind="operator_action",
                occurred_at=_datetime(item.get("occurred_at") or item.get("acted_at")),
                status=str(item.get("status") or "recorded"),
                summary=str(item.get("summary") or item.get("action") or "Operator action recorded"),
                details={key: value for key, value in item.items() if key not in {"summary", "action", "occurred_at", "acted_at"}},
            )
        )
    for record in effects:
        # EffectResult.attempt_history is the durable source for retries.  The
        # summary effect remains for compatibility and represents the current
        # journal record; attempt events expose each individual outcome.
        for attempt, result in enumerate(record.attempt_history, start=1):
            entries.append(
                TimelineEntry(
                    event_id=f"{record.command.effect_id}:attempt:{attempt}",
                    kind="effect_attempt",
                    occurred_at=result.completed_at,
                    status=result.status.value,
                    summary=f"{record.command.operation} attempt {attempt}",
                    details={
                        "effect_id": record.command.effect_id,
                        "idempotency_key": record.command.idempotency_key,
                        **({"provider_reference": result.provider_reference} if result.provider_reference else {}),
                        **({"error": result.error_message} if result.error_message else {}),
                    },
                )
            )
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
    # Records can be read from both a checkpoint and an append-only store.
    # Identity-based collapse makes a rebuild idempotent.
    by_id = {entry.event_id: entry for entry in entries}
    return tuple(
        sorted(
            by_id.values(),
            key=lambda entry: (
                entry.occurred_at or datetime.min.replace(tzinfo=UTC),
                entry.kind,
                entry.event_id,
            ),
        )
    )


def rebuild_execution_timeline(
    checkpoint: Mapping[str, Any],
    *,
    effects: Sequence[EffectRecord] = (),
    observation_decisions: Sequence[Any] = (),
    migrations: Sequence[Mapping[str, Any]] = (),
    operator_actions: Sequence[Mapping[str, Any]] = (),
    timeline_entries: Sequence[TimelineEntry] = (),
) -> tuple[TimelineEntry, ...]:
    """Rebuild the timeline solely from durable records.

    This explicit entry point is useful for audits and deterministic replay;
    it intentionally does not consult Jira, provider APIs, or worker logs.
    """
    decisions = tuple(observation_decisions) or _checkpoint_observation_decisions(checkpoint)
    return _timeline(
        checkpoint,
        effects,
        decisions,
        migrations=migrations,
        operator_actions=operator_actions,
        timeline_entries=timeline_entries,
    )
