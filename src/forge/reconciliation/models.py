"""Durable decisions made while reconciling external observations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from forge.domain import DomainModel, Observation, VersionedDomainModel


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


class ProcessingPhase(StrEnum):
    """Durable execution progress for one accepted observation delivery."""

    PENDING = "pending"
    RUNNING = "running"
    PREPARED = "prepared"
    APPLIED = "applied"
    IGNORED = "ignored"
    BLOCKED = "blocked"


class ObservationProcessingRecord(VersionedDomainModel):
    """Recoverable command handoff created atomically with observation acceptance.

    The serialized ingress message preserves provider-specific command evidence
    (for example a Jira changelog) across redelivery. ``phase`` describes the
    workflow handoff only; queue retry counts and delivery attempts remain
    transport metadata.
    """

    delivery_identity: str
    observation: Observation
    ticket_key: str
    ingress_message: dict[str, Any]
    phase: ProcessingPhase = ProcessingPhase.PENDING
    command_decision: dict[str, Any] | None = None
    command_id: str | None = None
    workflow_pin: dict[str, Any] | None = None
    expected_checkpoint_identity: str | None = None
    checkpoint_identity: str | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
