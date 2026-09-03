"""Stable, read-only contract consumed by Org Pulse dashboards.

Org Pulse must not need to understand checkpoint internals or provider payloads.
This contract is deliberately a compact summary of the execution read model and
contains no commands or mutation affordances.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from forge.domain import VersionedDomainModel
from forge.read_models.models import ExecutionReadModel


class OrgPulseExecution(VersionedDomainModel):
    """Dashboard-safe execution status, versioned independently of checkpoints."""

    run_id: str
    ticket_key: str
    status: str
    current_position: str
    workflow: str
    workflow_revision: int
    waiting_code: str | None = None
    waiting_since: datetime | None = None
    blocking_reason: str | None = None
    retry_count: int = Field(ge=0)
    observation_available: bool
    observation_stale: bool | None = None
    observation_conflicting: bool
    migration_eligible: bool | None = None
    migration_incompatibilities: tuple[str, ...] = ()

    @classmethod
    def from_execution(cls, execution: ExecutionReadModel) -> OrgPulseExecution:
        waiting = execution.waiting
        retries = sum(max(0, item.attempt - 1) for item in execution.station_attempts)
        retries += sum(max(0, item.attempt - 1) for item in execution.effects)
        return cls(
            run_id=execution.run_id,
            ticket_key=execution.ticket_key,
            status=execution.status.value,
            current_position=execution.current_position,
            workflow=execution.definition.name,
            workflow_revision=execution.definition.revision,
            waiting_code=waiting.code if waiting else None,
            waiting_since=waiting.since if waiting else None,
            blocking_reason=(
                waiting.message if waiting and execution.status.value == "blocked" else None
            ),
            retry_count=retries,
            observation_available=execution.last_observation.available,
            observation_stale=execution.last_observation.stale,
            observation_conflicting=execution.last_observation.conflicting,
            migration_eligible=execution.migration.eligible,
            migration_incompatibilities=execution.migration.incompatibilities,
        )


def pulse_timestamp(value: datetime | None) -> str | None:
    """Return a normalized timestamp for clients that serialize pulse records."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
