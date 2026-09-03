"""Durable decisions made while reconciling external observations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from forge.domain import DomainModel, Observation


class ObservationDisposition(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONFLICT = "conflict"


class DriftClass(StrEnum):
    EXPECTED = "expected"
    AUTO_RECONCILABLE = "auto_reconcilable"
    POLICY_BLOCKING = "policy_blocking"
    OPERATOR_REQUIRED = "operator_required"


class ObservationDecision(DomainModel):
    observation: Observation
    delivery_identity: str
    disposition: ObservationDisposition
    drift: DriftClass
    reason: str
    decided_at: datetime
    supersedes_delivery_identity: str | None = None


class ReconciledResource(DomainModel):
    latest: Observation
    latest_delivery_identity: str
    updated_at: datetime
