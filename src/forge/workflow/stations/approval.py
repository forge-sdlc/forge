"""Provider- and graph-independent human approval policy station."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from forge.domain import DomainModel, StationOutcome, StationOutcomeStatus, StationRequest

CONTRACT_NAME = "approval-policy"
CONTRACT_VERSION = "1.0"


class ApprovalDisposition(StrEnum):
    QUESTION = "question"
    APPROVED = "approved"
    REVISION = "revision"
    WAITING = "waiting"
    INVALID = "invalid"


class ApprovalInput(DomainModel):
    stage: str
    paused: bool = False
    yolo_mode: bool = False
    is_question: bool = False
    revision_requested: bool = False
    feedback: str | None = None
    item_count: int | None = None
    current_item: str | None = None


class ApprovalOutput(DomainModel):
    disposition: ApprovalDisposition
    revision_scope: str | None = None
    reason: str


def run_approval_station(
    request: StationRequest[ApprovalInput],
) -> StationOutcome[ApprovalOutput]:
    value = request.input
    if value.item_count is not None and value.item_count == 0:
        disposition = ApprovalDisposition.INVALID
        reason = "No reviewable items were produced"
        status = StationOutcomeStatus.RETRYABLE_FAILURE
        scope = None
    elif value.is_question and value.feedback:
        disposition = ApprovalDisposition.QUESTION
        reason = "Human requested clarification"
        status = StationOutcomeStatus.SUCCEEDED
        scope = None
    elif value.yolo_mode:
        disposition = ApprovalDisposition.APPROVED
        reason = "Approval policy permits automatic approval"
        status = StationOutcomeStatus.SUCCEEDED
        scope = None
    elif value.revision_requested and (value.feedback or value.current_item):
        disposition = ApprovalDisposition.REVISION
        scope = "item" if value.current_item else "all"
        reason = f"Human requested {scope} revision"
        status = StationOutcomeStatus.SUCCEEDED
    elif value.paused:
        disposition = ApprovalDisposition.WAITING
        reason = "Waiting for an eligible human command"
        status = StationOutcomeStatus.WAITING
        scope = None
    else:
        disposition = ApprovalDisposition.APPROVED
        reason = "Approval command was accepted"
        status = StationOutcomeStatus.SUCCEEDED
        scope = None
    return StationOutcome[ApprovalOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=status,
        completed_at=datetime.now(UTC),
        output=ApprovalOutput(
            disposition=disposition,
            revision_scope=scope,
            reason=reason,
        ),
        reason=reason,
    )
