"""Commands requesting evaluation of a workflow instance."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from forge.domain.identity import WorkflowIdentity
from forge.domain.schema import JsonValue, VersionedDomainModel


class WorkflowCommandType(StrEnum):
    START = "start"
    RESUME = "resume"
    APPROVE = "approve"
    REJECT = "reject"
    RETRY = "retry"
    CANCEL = "cancel"
    SYNCHRONIZE = "synchronize"


class WorkflowCommand(VersionedDomainModel):
    command_id: str = Field(min_length=1)
    command_type: WorkflowCommandType
    workflow: WorkflowIdentity
    requested_at: datetime
    observation_ids: tuple[str, ...] = ()
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    correlation: dict[str, JsonValue] = Field(default_factory=dict)
