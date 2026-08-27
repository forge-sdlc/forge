"""Pure conversion of normalized ingress evidence into workflow commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.domain import (
    CommentType,
    WorkflowCommand,
    WorkflowCommandType,
    WorkflowIdentity,
    classify_comment,
    stable_identity,
)
from forge.integrations.source_control.contracts import (
    ChangeRequestState,
    CheckStatus,
    EventKind,
    ReviewState,
)
from forge.models.events import EventSource
from forge.orchestrator.event_adapters.contracts import AdaptedEvent, IngressMessage


class CommandDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    INVALID = "invalid"
    STALE = "stale"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class CommandDecision:
    status: CommandDecisionStatus
    reason: str
    command: WorkflowCommand | None = None


def validate_command_decision(
    decision: CommandDecision, state: Mapping[str, Any]
) -> CommandDecision:
    """Classify a derived command against durable workflow state."""
    command = decision.command
    if command is None or decision.status is not CommandDecisionStatus.ACCEPTED:
        return decision
    if any(
        item.get("command_id") == command.command_id
        for item in state.get("command_decisions", [])
    ):
        return CommandDecision(CommandDecisionStatus.DUPLICATE, "command already decided", command)
    revision = state.get("workflow_definition_revision") or state.get("workflow_revision")
    if revision is not None and int(revision) != command.workflow.definition_revision:
        return CommandDecision(
            CommandDecisionStatus.STALE,
            "command targets a different workflow definition revision",
            command,
        )
    if state.get("workflow_status") == "cancelled":
        return CommandDecision(CommandDecisionStatus.INVALID, "workflow is cancelled", command)
    return decision


def record_command_decision(
    state: Mapping[str, Any],
    *,
    message: IngressMessage,
    adapted: AdaptedEvent,
    decision: CommandDecision,
    limit: int = 100,
) -> dict[str, Any]:
    """Append one idempotent, JSON-safe command decision to checkpoint state."""
    command = decision.command
    decision_id = stable_identity(
        "command-decision",
        {
            "event_id": message.event_id,
            "observation_id": adapted.observation.observation_id,
            "command_id": command.command_id if command else None,
            "status": decision.status.value,
        },
    )
    existing = list(state.get("command_decisions", []))
    if any(item.get("decision_id") == decision_id for item in existing):
        return dict(state)
    record = {
        "decision_id": decision_id,
        "decided_at": message.timestamp.isoformat(),
        "event_id": message.event_id,
        "observation_id": adapted.observation.observation_id,
        "status": decision.status.value,
        "reason": decision.reason,
        "command_id": command.command_id if command else None,
        "command_type": command.command_type.value if command else None,
    }
    return {**state, "command_decisions": [*existing, record][-limit:]}


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
        signal = _jira_signal(message, adapted, state)
    else:
        signal = _source_control_signal(adapted, state)
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
    message: IngressMessage, adapted: AdaptedEvent, state: Mapping[str, Any]
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
            return WorkflowCommandType.RETRY, {
                "stage": current_node,
                "source_system": "jira",
            }
        if (
            "forge:yolo" in after
            and "forge:yolo" not in before
            and current_node in {
                "prd_approval_gate",
                "spec_approval_gate",
                "plan_approval_gate",
                "task_plan_approval_gate",
                "task_approval_gate",
            }
        ):
            return WorkflowCommandType.ENABLE_YOLO, {
                "stage": current_node,
                "source_system": "jira",
            }
        if "approved" in after and "pending" in before:
            stage = next(
                (name for name in ("prd", "spec", "plan", "task") if f"{name}-approved" in after),
                None,
            )
            if stage and _NODE_APPROVAL_STAGE.get(current_node) == stage:
                return WorkflowCommandType.APPROVE, {
                    "stage": stage,
                    "source_system": "jira",
                }

    labels = {
        str(label).lower()
        for label in message.payload.get("issue", {}).get("fields", {}).get("labels", [])
    }
    approved_label = _GATE_APPROVED_LABEL.get(current_node)
    if approved_label and approved_label in labels:
        return WorkflowCommandType.APPROVE, {
            "stage": _NODE_APPROVAL_STAGE[current_node],
            "source_system": "jira",
        }

    if current_node in _PRD_GATE_NODES and state.get("prd_pr_number"):
        return None
    if current_node in _SPEC_GATE_NODES and state.get("spec_pr_number"):
        return None
    comment = adapted.observation.facts.get("comment_text", "")
    if isinstance(comment, str) and comment.strip():
        if comment.strip().lower().startswith("/forge cancel"):
            return WorkflowCommandType.CANCEL, {
                "source_system": "jira",
                "reason": comment.strip()[len("/forge cancel") :].strip() or None,
            }
        if current_node == "rca_option_gate":
            option_match = re.search(r">option\s+(\d+)", comment, re.IGNORECASE)
            if option_match:
                option = int(option_match.group(1))
                return WorkflowCommandType.SELECT_OPTION, {
                    "option": option,
                    "source_system": "jira",
                }
        source_ticket_key = adapted.observation.facts.get("source_ticket_key")
        issue = adapted.observation.facts.get("issue", {})
        issue_fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        issue_type = issue_fields.get("issuetype", {}).get("name", "")
        common = {
            "stage": current_node,
            "source_system": "jira",
            "source_ticket_key": str(source_ticket_key or "") or None,
            "source_ticket_type": str(issue_type).lower() or None,
        }
        classification = classify_comment(comment)
        if classification is CommentType.FEEDBACK:
            return WorkflowCommandType.REJECT, {
                **common,
                "feedback": re.sub(r"^\s*!\s*", "", comment),
            }
        if classification is CommentType.QUESTION:
            return WorkflowCommandType.RESUME, {
                **common,
                "question": comment,
            }
    return None


_PRD_GATE_NODES = {"prd_approval_gate", "generate_prd", "regenerate_prd"}
_SPEC_GATE_NODES = {"spec_approval_gate", "generate_spec", "regenerate_spec"}


def _source_control_signal(
    adapted: AdaptedEvent,
    state: Mapping[str, Any],
) -> tuple[WorkflowCommandType, dict[str, Any]] | None:
    event = adapted.normalized_event
    if event is None:
        return None
    if event.change_request and event.change_request.state is ChangeRequestState.MERGED:
        return WorkflowCommandType.APPROVE, {
            "reason": "change_request_merged",
            "source_system": event.repo_ref.provider.value,
        }
    if event.kind is EventKind.COMMENT_CREATED and event.comment is not None:
        comment = event.comment
        if comment.path is None:
            body = comment.body.strip()
            lowered = body.lower()
            current_node = str(state.get("current_node") or "")
            for prefix, command_type in (
                ("/forge skip-gate", WorkflowCommandType.SKIP_GATE),
                ("/forge unskip-gate", WorkflowCommandType.UNSKIP_GATE),
            ):
                if lowered.startswith(prefix):
                    check_name = body[len(prefix) :].strip()
                    if current_node not in {
                        "ci_evaluator",
                        "attempt_ci_fix",
                        "human_review_gate",
                    } or not check_name:
                        return None
                    return command_type, {
                        "check_name": check_name,
                        "stage": current_node,
                        "sender": event.actor.login,
                    }
            if lowered.startswith("/forge rebase") and state.get("current_pr_number"):
                return WorkflowCommandType.REBASE, {
                    "return_stage": current_node,
                    "sender": event.actor.login,
                }
            if lowered.startswith("/forge cancel"):
                return WorkflowCommandType.CANCEL, {
                    "source_system": event.repo_ref.provider.value,
                    "reason": body[len("/forge cancel") :].strip() or None,
                    "sender": event.actor.login,
                }
    if event.kind is EventKind.CHECK_UPDATED:
        if event.check_suite_status and event.check_suite_status is not CheckStatus.COMPLETED:
            return None
        return WorkflowCommandType.SYNCHRONIZE, {
            "subject": "checks",
            "source_system": event.repo_ref.provider.value,
        }
    if event.kind is EventKind.REVIEW_SUBMITTED and event.review is not None:
        review = event.review
        common = {
            "source_system": event.repo_ref.provider.value,
            "review_id": review.id,
            "sender": review.author,
        }
        if review.state is ReviewState.APPROVED:
            return WorkflowCommandType.APPROVE, {**common, "reason": "review_approved"}
        if review.state in {ReviewState.CHANGES_REQUESTED, ReviewState.COMMENTED}:
            return WorkflowCommandType.REJECT, {
                **common,
                "feedback": review.body,
                "requires_thread_enrichment": True,
            }
        return None
    if event.kind is EventKind.COMMENT_CREATED and event.comment is not None:
        body = event.comment.body.strip()
        common = {
            "source_system": event.repo_ref.provider.value,
            "comment_id": event.comment.id,
            "sender": event.actor.login,
            "path": event.comment.path,
            "in_reply_to": event.comment.in_reply_to,
        }
        classification = classify_comment(body)
        if classification is CommentType.QUESTION:
            return WorkflowCommandType.RESUME, {**common, "question": body}
        if classification is CommentType.FEEDBACK or event.comment.path is not None:
            return WorkflowCommandType.REJECT, {
                **common,
                "feedback": re.sub(r"^\s*!\s*", "", body),
                "requires_thread_enrichment": event.comment.path is not None,
            }
    return None
