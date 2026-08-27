"""Observations of external state supplied through any ingress path."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain.identity import ResourceIdentity
from forge.domain.schema import JsonValue, VersionedDomainModel


class ObservationSource(StrEnum):
    WEBHOOK = "webhook"
    POLLER = "poller"
    INTERNAL = "internal"


class Observation(VersionedDomainModel):
    observation_id: str = Field(min_length=1)
    source: ObservationSource
    source_system: str = Field(min_length=1)
    resource: ResourceIdentity
    resource_revision: str | None = None
    observed_at: datetime
    received_at: datetime
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    correlation: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_reference: str | None = None
