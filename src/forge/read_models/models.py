"""Versioned, execution-neutral operator read models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain import JsonValue, VersionedDomainModel


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


class DefinitionView(VersionedDomainModel):
    name: str
    revision: int
    digest: str | None = None
    available: bool
    manifest: dict[str, JsonValue] | None = None


class WaitingView(VersionedDomainModel):
    code: str
    message: str
    since: datetime | None = None
    recovery: str | None = None


class NextTransitionView(VersionedDomainModel):
    outcome: str | None = None
    target: str


class ObservationView(VersionedDomainModel):
    observation_id: str | None = None
    source_system: str | None = None
    observed_at: datetime | None = None
    stale: bool | None = None
    conflicting: bool = False
    available: bool
    disposition: str | None = None
    reason: str | None = None
    resource_revision: str | None = None
    revision_order: int | None = None


class RuleClauseView(VersionedDomainModel):
    """The result of evaluating one persisted workflow rule clause.

    Both satisfied and unsatisfied clauses are retained.  In particular, an
    operator must be able to see which prerequisite was false rather than
    reverse-engineering a reason from the current node name.
    """

    capability: str
    satisfied: bool
    on_missing: str | None = None
    reason: str | None = None


class RuleExplanationView(VersionedDomainModel):
    rule: str
    node: str
    satisfied: bool
    action: str | None = None
    summary: str
    clauses: tuple[RuleClauseView, ...] = ()


class RecoveryOptionView(VersionedDomainModel):
    command: str
    description: str
    available: bool = True


class EffectAttemptView(VersionedDomainModel):
    status: str
    completed_at: datetime
    provider_reference: str | None = None
    error: str | None = None


class StationAttemptView(VersionedDomainModel):
    station_name: str
    invocation_id: str
    attempt: int
    status: str
    completed_at: datetime | None = None
    reason: str | None = None


class EffectView(VersionedDomainModel):
    effect_id: str
    operation: str
    target: str
    status: str
    attempt: int
    updated_at: datetime
    provider_reference: str | None = None
    error: str | None = None
    attempts: tuple[EffectAttemptView, ...] = ()


class MigrationView(VersionedDomainModel):
    eligible: bool | None = None
    incompatibilities: tuple[str, ...] = ()


class TimelineEntry(VersionedDomainModel):
    event_id: str
    kind: str
    occurred_at: datetime | None = None
    status: str | None = None
    summary: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TimelinePage(VersionedDomainModel):
    items: tuple[TimelineEntry, ...]
    next_cursor: int | None = None
    total: int = 0


class ExecutionReadModel(VersionedDomainModel):
    run_id: str
    ticket_key: str
    status: ExecutionStatus
    current_position: str
    definition: DefinitionView
    permitted_commands: tuple[str, ...]
    next_transitions: tuple[NextTransitionView, ...]
    waiting: WaitingView | None = None
    last_observation: ObservationView
    stale_observations: tuple[ObservationView, ...] = ()
    conflicting_observations: tuple[ObservationView, ...] = ()
    station_attempts: tuple[StationAttemptView, ...] = ()
    effects: tuple[EffectView, ...] = ()
    recovery_options: tuple[RecoveryOptionView, ...] = ()
    explanations: tuple[RuleExplanationView, ...] = ()
    migration: MigrationView = MigrationView()
    timeline: tuple[TimelineEntry, ...] = ()
