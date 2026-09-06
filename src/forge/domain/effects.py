"""Provider-neutral external-effect intent and result contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain.identity import ResourceIdentity, WorkflowIdentity
from forge.domain.schema import JsonValue, VersionedDomainModel


class EffectResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PRECONDITION_FAILED = "precondition_failed"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


class EffectCommand(VersionedDomainModel):
    effect_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    workflow: WorkflowIdentity
    operation: str = Field(min_length=1)
    target: ResourceIdentity
    expected_precondition: dict[str, JsonValue] = Field(default_factory=dict)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class EffectResult(VersionedDomainModel):
    effect_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: EffectResultStatus
    completed_at: datetime
    provider_reference: str | None = None
    output: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
