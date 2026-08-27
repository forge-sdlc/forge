"""Persisted state of one external effect."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain import DomainModel, EffectCommand, EffectResult


class EffectRecordStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PRECONDITION_FAILED = "precondition_failed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


class EffectRecord(DomainModel):
    command: EffectCommand
    status: EffectRecordStatus
    attempt: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime
    lease_until: datetime | None = None
    result: EffectResult | None = None
    attempt_history: list[EffectResult] = Field(default_factory=list)
    replay_count: int = Field(default=0, ge=0)
