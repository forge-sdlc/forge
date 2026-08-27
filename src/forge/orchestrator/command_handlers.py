"""Provider-neutral application of exceptional workflow commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from forge.domain import WorkflowCommand, WorkflowCommandType


class FeedbackKind(StrEnum):
    SKIP_GATE = "skip_gate"
    REBASE = "rebase"
    RETRY_ACKNOWLEDGEMENT = "retry_acknowledgement"
    TERMINAL_ERROR = "terminal_error"
    RESUME_ACKNOWLEDGEMENT = "resume_acknowledgement"
    OPTION_RANGE = "option_range"


@dataclass(frozen=True)
class FeedbackRequest:
    kind: FeedbackKind
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CommandApplication:
    state: dict[str, Any]
    feedback: FeedbackRequest | None = None


CommandHandler = Callable[[WorkflowCommand, Mapping[str, Any]], CommandApplication | None]


class CommandHandlerRegistry:
    """Select command application by type without inspecting ingress payloads."""

    def __init__(self) -> None:
        self._handlers: dict[WorkflowCommandType, CommandHandler] = {}

    def register(self, command_type: WorkflowCommandType, handler: CommandHandler) -> None:
        if command_type in self._handlers:
            raise ValueError(f"Handler already registered for {command_type.value}")
        self._handlers[command_type] = handler

    def apply(
        self, command: WorkflowCommand, state: Mapping[str, Any]
    ) -> CommandApplication | None:
        handler = self._handlers.get(command.command_type)
        return handler(command, state) if handler else None


_CI_STAGES = {"ci_evaluator", "attempt_ci_fix", "human_review_gate"}


def _apply_skip_gate(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication | None:
    current_node = str(state.get("current_node") or "")
    if current_node not in _CI_STAGES:
        return None
    check_name = str(command.arguments.get("check_name") or "").strip()
    if not check_name:
        return None
    skipped = list(state.get("ci_skipped_checks", []))
    if command.command_type is WorkflowCommandType.SKIP_GATE:
        if check_name not in skipped:
            skipped.append(check_name)
        action = "skip"
    else:
        skipped = [item for item in skipped if item != check_name]
        action = "unskip"
    return CommandApplication(
        state={
            **state,
            "ci_skipped_checks": skipped,
            "is_paused": False,
            # Compatibility transition until Phase 5 owns topology.
            "current_node": "ci_evaluator",
        },
        feedback=FeedbackRequest(
            FeedbackKind.SKIP_GATE,
            {
                "check_name": check_name,
                "sender": command.arguments.get("sender"),
                "action": action,
            },
        ),
    )


def _apply_rebase(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication | None:
    if not state.get("current_pr_number"):
        return None
    current_node = str(state.get("current_node") or "")
    return CommandApplication(
        state={
            **state,
            "rebase_return_node": current_node,
            "is_paused": False,
            # Compatibility transition until Phase 5 owns topology.
            "current_node": "rebase_pr",
        },
        feedback=FeedbackRequest(
            FeedbackKind.REBASE, {"sender": command.arguments.get("sender")}
        ),
    )


def _apply_yolo(
    _command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication:
    return CommandApplication(
        state={
            **state,
            "yolo_mode": True,
            "is_paused": False,
            "revision_requested": False,
            "feedback_comment": None,
            "last_error": None,
        }
    )


def _apply_select_option(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication | None:
    option = command.arguments.get("option")
    options = list(state.get("rca_options", []))
    if not isinstance(option, int) or not 1 <= option <= len(options):
        return CommandApplication(
            state=state if isinstance(state, dict) else dict(state),
            feedback=FeedbackRequest(
                FeedbackKind.OPTION_RANGE, {"maximum": len(options)}
            ),
        )
    return CommandApplication(
        state={
            **state,
            "selected_fix_option": option,
            "selected_fix_approach": options[option - 1],
            "is_paused": False,
            "is_question": False,
            "revision_requested": False,
            "feedback_comment": None,
        }
    )


def _apply_retry(
    _command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication:
    current_node = str(state.get("current_node") or "")
    if current_node == "complete":
        return CommandApplication(
            state=dict(state),
            feedback=FeedbackRequest(
                FeedbackKind.TERMINAL_ERROR,
                {"message": "Workflow is already complete — nothing to retry."},
            ),
        )

    updated = {
        **state,
        "is_paused": False,
        "is_blocked": False,
        "last_error": None,
        "auto_retry_cap_notified": False,
        "retry_count": 0,
    }
    approval_gates = {
        "prd_approval_gate",
        "spec_approval_gate",
        "plan_approval_gate",
        "task_approval_gate",
        "plan_approval_gate_bug",
        "task_plan_approval_gate",
    }
    if current_node == "triage_gate":
        updated["current_node"] = "triage_check"
        updated["context"] = {**state.get("context", {}), "force_fresh_invoke": True}
    elif current_node == "review_response_gate":
        updated.update(
            {
                "revision_requested": False,
                "feedback_comment": None,
                "contested_comments": [],
                "current_node": "human_review_gate",
                "context": {**state.get("context", {}), "force_fresh_invoke": True},
            }
        )
    elif state.get("is_paused") and current_node in approval_gates:
        updated.update(
            {
                "revision_requested": True,
                "feedback_comment": "Regeneration requested via retry.",
                "current_epic_key": None,
                "current_task_key": None,
            }
        )
    else:
        updated.update(
            {
                "revision_requested": False,
                "feedback_comment": None,
                "ci_fix_attempt": 0,
                "context": {**state.get("context", {}), "force_fresh_invoke": True},
            }
        )
    return CommandApplication(
        state=updated,
        feedback=FeedbackRequest(
            FeedbackKind.RETRY_ACKNOWLEDGEMENT,
            {"stage": updated.get("current_node", current_node)},
        ),
    )


def _apply_approval(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication | None:
    if command.arguments.get("source_system") != "jira":
        return None
    return CommandApplication(
        state={
            **state,
            "is_paused": False,
            "revision_requested": False,
            "feedback_comment": None,
            "last_error": None,
        }
    )


def _source_ticket(command: WorkflowCommand, state: Mapping[str, Any]) -> tuple[str | None, str | None]:
    source_key = command.arguments.get("source_ticket_key")
    if not isinstance(source_key, str) or not source_key:
        return None, None
    current_node = str(state.get("current_node") or "")
    plan_nodes = {
        "plan_approval_gate",
        "decompose_epics",
        "regenerate_all_epics",
        "update_single_epic",
    }
    task_nodes = {
        "task_approval_gate",
        "generate_tasks",
        "regenerate_all_tasks",
        "regenerate_epic_tasks",
        "update_single_task",
    }
    if current_node in plan_nodes and source_key in state.get("epic_keys", []):
        return source_key, "epic"
    if current_node in task_nodes:
        if source_key in state.get("task_keys", []):
            return source_key, "task"
        if source_key in state.get("epic_keys", []):
            return source_key, "epic"
    return None, None


def _apply_feedback(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> CommandApplication | None:
    if command.arguments.get("source_system") != "jira":
        return None
    is_question = command.command_type is WorkflowCommandType.RESUME
    content_key = "question" if is_question else "feedback"
    content = str(command.arguments.get(content_key) or "").strip()
    if not content:
        return None
    source_key, source_type = _source_ticket(command, state)
    updated = {
        **state,
        "is_paused": False,
        "is_question": is_question,
        "revision_requested": not is_question,
        "feedback_comment": content,
    }
    if not is_question:
        if state.get("current_node") == "review_response_gate":
            updated["contested_comments"] = []
        if source_type == "epic":
            updated["current_epic_key"] = source_key
            updated["current_task_key"] = None
        elif source_type == "task":
            updated["current_task_key"] = source_key
            updated["current_epic_key"] = None
        else:
            updated["current_epic_key"] = None
            updated["current_task_key"] = None
    return CommandApplication(
        state=updated,
        feedback=FeedbackRequest(
            FeedbackKind.RESUME_ACKNOWLEDGEMENT,
            {
                "signal_type": "question" if is_question else "revision",
                "stage": state.get("current_node", ""),
                "source_ticket_key": source_key,
            },
        ),
    )


def create_default_command_handler_registry() -> CommandHandlerRegistry:
    registry = CommandHandlerRegistry()
    registry.register(WorkflowCommandType.SKIP_GATE, _apply_skip_gate)
    registry.register(WorkflowCommandType.UNSKIP_GATE, _apply_skip_gate)
    registry.register(WorkflowCommandType.REBASE, _apply_rebase)
    registry.register(WorkflowCommandType.ENABLE_YOLO, _apply_yolo)
    registry.register(WorkflowCommandType.SELECT_OPTION, _apply_select_option)
    registry.register(WorkflowCommandType.RETRY, _apply_retry)
    registry.register(WorkflowCommandType.APPROVE, _apply_approval)
    registry.register(WorkflowCommandType.REJECT, _apply_feedback)
    registry.register(WorkflowCommandType.RESUME, _apply_feedback)
    return registry
