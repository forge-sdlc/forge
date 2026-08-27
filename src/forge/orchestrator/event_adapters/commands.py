"""Pure conversion of normalized ingress evidence into workflow commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.domain import (
    WorkflowCommand,
    WorkflowCommandType,
    WorkflowIdentity,
    stable_identity,
)
from forge.integrations.source_control.contracts import ChangeRequestState, EventKind
from forge.models.events import EventSource
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, IngressMessage
from forge.workflow.utils.comment_classifier import CommentType, classify_comment


class CommandDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"


@dataclass(frozen=True)
class CommandDecision:
    status: CommandDecisionStatus
    reason: str
    command: WorkflowCommand | None = None


_NODE_APPROVAL_STAGE = {
    "prd_approval_gate": "prd",
    "generate_prd": "prd",
    "regenerate_prd": "prd",
    "spec_approval_gate": "spec",
    "generate_spec": "spec",
    "regenerate_spec": "spec",
    "plan_approval_gate": "plan",
    "decompose_epics": "plan",
    "regenerate_all_epics": "plan",
    "update_single_epic": "plan",
    "task_plan_approval_gate": "plan",
    "task_approval_gate": "task",
    "generate_tasks": "task",
}
_GATE_APPROVED_LABEL = {
    "prd_approval_gate": "forge:prd-approved",
    "spec_approval_gate": "forge:spec-approved",
    "plan_approval_gate": "forge:plan-approved",
    "task_plan_approval_gate": "forge:plan-approved",
    "task_approval_gate": "forge:task-approved",
}


def interpret_event(
    message: IngressMessage,
    adapted: AdaptedEvent,
    state: Mapping[str, Any],
) -> CommandDecision:
    """Derive one idempotent command without selecting or mutating a graph node."""
    if message.source is EventSource.JIRA:
        signal = _jira_signal(message, state)
    else:
        signal = _source_control_signal(adapted)
    if signal is None:
        return CommandDecision(CommandDecisionStatus.IGNORED, "no eligible workflow signal")

    command_type, arguments = signal
    workflow = _workflow_identity(message, state)
    command_id = stable_identity(
        "workflow-command",
        {
            "event_id": message.event_id,
            "run_id": workflow.run_id,
            "command_type": command_type.value,
        },
    )
    command = WorkflowCommand(
        command_id=command_id,
        command_type=command_type,
        workflow=workflow,
        requested_at=message.timestamp,
        observation_ids=(adapted.observation.observation_id,),
        arguments=arguments,
        correlation={"transport_event_id": message.event_id},
    )
    return CommandDecision(CommandDecisionStatus.ACCEPTED, "eligible signal", command)


def _workflow_identity(message: IngressMessage, state: Mapping[str, Any]) -> WorkflowIdentity:
    revision = state.get("workflow_definition_revision") or state.get("workflow_revision") or 1
    return WorkflowIdentity(
        run_id=str(state.get("thread_id") or state.get("ticket_key") or message.ticket_key),
        workflow_name=str(state.get("workflow_name") or state.get("ticket_type") or "legacy"),
        definition_revision=int(revision),
        definition_digest=state.get("workflow_definition_digest"),
    )


def _jira_signal(
    message: IngressMessage, state: Mapping[str, Any]
) -> tuple[WorkflowCommandType, dict[str, Any]] | None:
    current_node = str(state.get("current_node") or "")
    changes = [
        item
        for item in message.payload.get("changelog", {}).get("items", [])
        if item.get("field") == "labels"
    ]
    for change in changes:
        before = str(change.get("fromString") or "").lower()
        after = str(change.get("toString") or "").lower()
        if "forge:retry" in after and "forge:retry" not in before:
            return WorkflowCommandType.RETRY, {"stage": current_node}
        if "approved" in after and "pending" in before:
            stage = next(
                (name for name in ("prd", "spec", "plan", "task") if f"{name}-approved" in after),
                None,
            )
            if stage and _NODE_APPROVAL_STAGE.get(current_node) == stage:
                return WorkflowCommandType.APPROVE, {"stage": stage}

    labels = {
        str(label).lower()
        for label in message.payload.get("issue", {}).get("fields", {}).get("labels", [])
    }
    approved_label = _GATE_APPROVED_LABEL.get(current_node)
    if approved_label and approved_label in labels:
        return WorkflowCommandType.APPROVE, {"stage": _NODE_APPROVAL_STAGE[current_node]}

    comment = message.payload.get("comment", {}).get("body", "")
    if isinstance(comment, str) and comment.strip():
        classification = classify_comment(comment)
        if classification is CommentType.FEEDBACK:
            return WorkflowCommandType.REJECT, {
                "feedback": re.sub(r"^\s*!\s*", "", comment),
                "stage": current_node,
            }
        if classification is CommentType.QUESTION:
            return WorkflowCommandType.RESUME, {
                "question": comment,
                "stage": current_node,
            }
    return None


def _source_control_signal(
    adapted: AdaptedEvent,
) -> tuple[WorkflowCommandType, dict[str, Any]] | None:
    event = adapted.normalized_event
    if event is None:
        return None
    if event.change_request and event.change_request.state is ChangeRequestState.MERGED:
        return WorkflowCommandType.APPROVE, {"reason": "change_request_merged"}
    if event.kind is EventKind.CHECK_UPDATED:
        return WorkflowCommandType.SYNCHRONIZE, {"subject": "checks"}
    if event.kind in {EventKind.REVIEW_SUBMITTED, EventKind.COMMENT_CREATED}:
        return WorkflowCommandType.SYNCHRONIZE, {"subject": "review"}
    return None
