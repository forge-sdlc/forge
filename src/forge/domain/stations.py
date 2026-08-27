"""Typed invocation and outcome contracts for independently runnable stations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import Field

from forge.domain.effects import EffectCommand
from forge.domain.identity import StationInvocationIdentity, WorkflowIdentity
from forge.domain.schema import DomainModel, JsonValue, VersionedDomainModel

InputT = TypeVar("InputT", bound=DomainModel)
OutputT = TypeVar("OutputT", bound=DomainModel)


class StationOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    WAITING = "waiting"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


class StationFailure(DomainModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, JsonValue] = Field(default_factory=dict)


class StationRequest(VersionedDomainModel, Generic[InputT]):
    workflow: WorkflowIdentity
    invocation: StationInvocationIdentity
    contract_name: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    requested_at: datetime
    deadline: datetime | None = None
    artifact_references: tuple[str, ...] = ()
    policy_context: dict[str, JsonValue] = Field(default_factory=dict)
    input: InputT


class StationOutcome(VersionedDomainModel, Generic[OutputT]):
    workflow: WorkflowIdentity
    invocation: StationInvocationIdentity
    contract_name: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    status: StationOutcomeStatus
    completed_at: datetime
    output: OutputT | None = None
    requested_effects: tuple[EffectCommand, ...] = ()
    reason: str | None = None
    failure: StationFailure | None = None
