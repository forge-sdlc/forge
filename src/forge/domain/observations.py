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
        return observation_delivery_identity(self)


def observation_delivery_identity(observation: Observation) -> str:
    """Return the source-independent identity of an observation delivery.

    A provider revision is the strongest identity available: delivery IDs are
    transport metadata and differ when the same state arrives from a webhook
    and from the poller.  For event-shaped resources that do not expose a
    revision, the provider event ID (stored in correlation metadata) keeps
    distinct events from collapsing into one delivery.  ``observation_id`` is
    the final fallback for callers constructing an observation without either
    kind of provider identity.

    ``revision_order`` is deliberately not included when ``resource_revision``
    is present.  It is ordering metadata, not part of the provider revision;
    including it would make equivalent deliveries deduplicate differently.
    """
    parts: dict[str, JsonValue] = {
        "source_system": observation.source_system,
        "resource_type": observation.resource.resource_type,
        "external_id": observation.resource.external_id,
        "namespace": observation.resource.namespace,
    }
    if observation.resource_revision is not None:
        parts["resource_revision"] = observation.resource_revision
    elif observation.revision_order is not None:
        parts["revision_order"] = observation.revision_order
    else:
        provider_event_id = observation.correlation.get("provider_event_id")
        if not isinstance(provider_event_id, str):
            provider_event_id = observation.correlation.get("transport_event_id")
        if isinstance(provider_event_id, str) and provider_event_id:
            parts["provider_event_id"] = provider_event_id
        else:
            parts["observation_id"] = observation.observation_id
    return stable_identity("observation-delivery", parts)


def observation_identity(
    *,
    source_system: str,
    provider_event_id: str,
    resource: ResourceIdentity,
    resource_revision: str | None = None,
) -> str:
    """Build the deterministic identity assigned to a provider observation.

    This identity remains stable when the delivery source changes.  The event
    ID distinguishes separate provider events, while the resource revision is
    included as context for providers that reuse event IDs across resources.
    """
    return stable_identity(
        "observation",
        {
            "source_system": source_system,
            "provider_event_id": provider_event_id,
            "resource_type": resource.resource_type,
            "external_id": resource.external_id,
            "namespace": resource.namespace,
            "resource_revision": resource_revision,
        },
    )
