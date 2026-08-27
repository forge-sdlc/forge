"""Observations of external state supplied through any ingress path."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain.identity import ResourceIdentity, stable_identity
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
    revision_order: int | None = Field(default=None, ge=0)
    observed_at: datetime
    received_at: datetime
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    correlation: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_reference: str | None = None

    @property
    def delivery_identity(self) -> str:
        """Identity shared by poller and webhook deliveries of the same revision."""
        return stable_identity(
            "observation-delivery",
            {
                "source_system": self.source_system,
                "resource_type": self.resource.resource_type,
                "external_id": self.resource.external_id,
                "namespace": self.resource.namespace,
                "resource_revision": self.resource_revision,
                "revision_order": self.revision_order,
            },
        )
