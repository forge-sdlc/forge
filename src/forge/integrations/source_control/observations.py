"""Compatibility adapter from source-control events to Forge observations."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from forge.domain import (
    JsonValue,
    Observation,
    ObservationSource,
    ResourceIdentity,
    observation_identity,
)
from forge.integrations.source_control.contracts import NormalizedEvent


def _json_value(value: Any) -> JsonValue:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported observation fact type: {type(value).__name__}")


def normalized_event_to_observation(
    event: NormalizedEvent,
    *,
    source: ObservationSource = ObservationSource.WEBHOOK,
) -> Observation:
    """Convert an existing transport event without retaining its raw payload."""
    change_request = event.change_request
    native_id = change_request.identity.native_id if change_request else None
    external_id = event.repo_ref.id
    resource_type = "repository"
    revision: str | None = None
    # Event-specific resources must win over the optional change-request
    # context attached by GitHub's check/comment/review payloads.  Otherwise
    # every check or comment on one head SHA is misidentified as that PR state.
    if event.check:
        resource_type = "check"
        external_id = f"{event.repo_ref.id}:{event.check.name}"
        revision = _check_revision(event)
    elif event.comment:
        resource_type = "comment"
        external_id = f"{event.repo_ref.id}:{event.comment.id}"
        revision = event.comment.id
    elif event.review:
        resource_type = "review"
        external_id = f"{event.repo_ref.id}:{event.review.id}"
        revision = event.review.id
    elif change_request:
        resource_type = "change_request"
        external_id = f"{event.repo_ref.id}#{native_id}"
        revision = change_request.head_sha or None

    facts = _json_value(
        {
            "kind": event.kind,
            "repository": event.repo_ref,
            "actor": event.actor,
            "change_request": event.change_request,
            "comment": event.comment,
            "review": event.review,
            "check": event.check,
            "check_suite_status": event.check_suite_status,
        }
    )
    assert isinstance(facts, dict)
    resource = ResourceIdentity(
        resource_type=resource_type,
        external_id=external_id,
        namespace=event.repo_ref.connection,
    )
    observation_id = observation_identity(
        source_system=event.repo_ref.provider.value,
        provider_event_id=event.id,
        resource=resource,
        resource_revision=revision,
    )
    return Observation(
        observation_id=observation_id,
        source=source,
        source_system=event.repo_ref.provider.value,
        resource=resource,
        resource_revision=revision,
        observed_at=event.received_at,
        received_at=event.received_at,
        facts=facts,
        correlation={
            "provider_event_id": event.id,
            "transport_event_id": event.id,
            "repository_id": event.repo_ref.id,
        },
        evidence_reference=f"source-control-event:{event.id}" if event.raw else None,
    )


def _check_revision(event: NormalizedEvent) -> str:
    """Return a revision for a check state, scoped to the checked commit.

    ``CheckRun`` intentionally keeps only provider-neutral fields.  Combining
    those fields with the associated change-request head prevents a successful
    check on two commits from being treated as one revision.  For standalone
    checks, the event ID is the provider's only available immutable identity.
    """
    assert event.check is not None
    head_sha = event.change_request.head_sha if event.change_request else ""
    scope = head_sha or event.id
    return f"{scope}:{event.check.status.value}:{event.check.conclusion.value}"
