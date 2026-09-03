"""Stable identities shared by workflow, station and effect contracts."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from forge.domain.schema import DomainModel, JsonValue


def stable_identity(namespace: str, parts: dict[str, JsonValue]) -> str:
    """Derive a deterministic identity from canonical JSON data."""
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return f"{namespace}:{digest}"


class WorkflowIdentity(DomainModel):
    run_id: str = Field(min_length=1)
    workflow_name: str = Field(min_length=1)
    definition_revision: int = Field(ge=1)
    definition_digest: str | None = None


class ResourceIdentity(DomainModel):
    resource_type: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    namespace: str | None = None


class StationInvocationIdentity(DomainModel):
    invocation_id: str = Field(min_length=1)
    station_name: str = Field(min_length=1)
