"""Persisted state of one external effect."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from forge.domain import DomainModel, EffectCommand, EffectResult


class EffectReconciliationDecision(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"


class EffectReconciliation(DomainModel):
    decision: EffectReconciliationDecision
    occurred_at: datetime
    note: str
    provider_reference: str | None = None


class EffectRecordStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PRECONDITION_FAILED = "precondition_failed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    INDETERMINATE = "indeterminate"


class EffectRecord(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    command: EffectCommand
    status: EffectRecordStatus
    attempt: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime
    lease_until: datetime | None = None
    # A fresh opaque fence for each claimed attempt. It is deliberately
    # separate from the stable provider idempotency key.
    claim_token: str | None = None
    # Set only when this claim took over a record whose prior RUNNING lease
    # expired. The service uses it to avoid blindly replaying unsafe writes.
    recovered_after_expiry: bool = False
    result: EffectResult | None = None
    attempt_history: list[EffectResult] = Field(default_factory=list)
    reconciliation_history: list[EffectReconciliation] = Field(default_factory=list)
    replay_count: int = Field(default=0, ge=0)
