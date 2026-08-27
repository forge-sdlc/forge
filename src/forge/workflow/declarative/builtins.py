"""Versioned definitions for Forge-supported golden paths."""

from __future__ import annotations

from typing import Any

from forge.models.workflow import TicketType
from forge.workflow.declarative.models import WorkflowDefinition
from forge.workflow.declarative.workflow import DeclarativeWorkflow

POLICY = "forge-contracts-v1"
JIRA_EFFECTS = ("jira.*",)
SC_EFFECTS = ("source_control.*",)


def _next(target: str, *, kind: str = "operation", effects: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "next": target,
        "kind": kind,
        "requiredPolicies": [POLICY],
        "allowedEffects": list(effects),
    }


def _route(
    router: str,
    branches: dict[str, str],
    *,
    kind: str = "operation",
    effects: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "route": router,
        "branches": branches,
        "kind": kind,
        "requiredPolicies": [POLICY],
        "allowedEffects": list(effects),
    }


def builtin_feature_definition() -> WorkflowDefinition:
    """Return the immutable feature golden-path definition."""
    steps = {
        "generate_prd": _route(
            "route_after_generation",
            {"prd_approval_gate": "prd_approval_gate", "__end__": "__end__"},
        ),
        "prd_approval_gate": _route(
            "route_prd_approval",
            {
                "generate_spec": "generate_spec",
                "regenerate_prd": "regenerate_prd",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "regenerate_prd": _route(
            "route_after_prd_regeneration",
            {"prd_approval_gate": "prd_approval_gate", "__end__": "__end__"},
        ),
        "generate_spec": _route(
            "route_after_spec_generation",
            {"spec_approval_gate": "spec_approval_gate", "__end__": "__end__"},
        ),
        "spec_approval_gate": _route(
            "route_spec_approval",
            {
                "decompose_epics": "decompose_epics",
                "regenerate_spec": "regenerate_spec",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "regenerate_spec": _route(
            "route_after_spec_regeneration",
            {"spec_approval_gate": "spec_approval_gate", "__end__": "__end__"},
        ),
        "decompose_epics": _route(
            "route_after_epic_decomposition",
            {"plan_approval_gate": "plan_approval_gate", "__end__": "__end__"},
        ),
        "plan_approval_gate": _route(
            "route_plan_approval",
            {
                "generate_tasks": "generate_tasks",
                "regenerate_all_epics": "regenerate_all_epics",
                "update_single_epic": "update_single_epic",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "regenerate_all_epics": _route(
            "route_after_epic_regeneration",
            {"plan_approval_gate": "plan_approval_gate", "__end__": "__end__"},
        ),
        "update_single_epic": _route(
            "route_after_single_epic_update",
            {"plan_approval_gate": "plan_approval_gate", "__end__": "__end__"},
        ),
        "generate_tasks": _route(
            "route_after_task_generation",
            {"task_approval_gate": "task_approval_gate", "__end__": "__end__"},
        ),
        "task_approval_gate": _route(
            "route_task_approval",
            {
                "task_router": "task_router",
                "regenerate_all_tasks": "regenerate_all_tasks",
                "regenerate_epic_tasks": "regenerate_epic_tasks",
                "update_single_task": "update_single_task",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "regenerate_all_tasks": _route(
            "route_after_task_regeneration",
            {"task_approval_gate": "task_approval_gate", "__end__": "__end__"},
        ),
        "update_single_task": _route(
            "route_after_single_task_update",
            {"task_approval_gate": "task_approval_gate", "__end__": "__end__"},
        ),
        "regenerate_epic_tasks": _route(
            "route_after_epic_task_regeneration",
            {"task_approval_gate": "task_approval_gate", "__end__": "__end__"},
        ),
        "task_router": {
            "route": "route_tasks_parallel",
            "dynamicRoute": True,
            "dynamicTargets": ["setup_workspace"],
            "kind": "station",
            "stationContract": "task-routing",
            "stationContractVersion": "1.0",
            "requiredPolicies": [POLICY],
            "maxConcurrency": 16,
        },
        "setup_workspace": _route(
            "route_after_workspace_setup",
            {"implement_task": "implement_task", "escalate_blocked": "escalate_blocked"},
        ),
        "implement_task": _route(
            "route_implementation",
            {
                "implement_task": "implement_task",
                "local_review": "local_review",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 100},
        "local_review": _route(
            "route_current_node",
            {
                "local_review": "local_review",
                "create_pr": "update_documentation",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 2},
        "update_documentation": _next("create_pr"),
        "create_pr": _route(
            "route_after_pr_creation",
            {"teardown_workspace": "teardown_workspace", "escalate_blocked": "escalate_blocked"},
        ),
        "teardown_workspace": _route(
            "route_after_teardown",
            {"setup_workspace": "setup_workspace", "human_review_gate": "human_review_gate"},
        ),
        "ci_evaluator": _route(
            "route_ci_evaluation",
            {
                "human_review_gate": "human_review_gate",
                "attempt_ci_fix": "attempt_ci_fix",
                "escalate_blocked": "escalate_blocked",
            },
        ),
        "attempt_ci_fix": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "escalate_blocked": "escalate_blocked",
                "ci_evaluator": "ci_evaluator",
                "attempt_ci_fix": "escalate_blocked",
            },
        )
        | {"retryBound": 5},
        "human_review_gate": _route(
            "route_human_review",
            {
                "ci_evaluator": "ci_evaluator",
                "implement_review": "implement_review",
                "complete_tasks": "complete_tasks",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "implement_review": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "review_response_gate": "review_response_gate",
                "implement_review": "implement_review",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 3},
        "review_response_gate": _route(
            "route_review_response",
            {
                "implement_review": "implement_review",
                "human_review_gate": "human_review_gate",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "complete_tasks": _next("aggregate_epic_status", effects=JIRA_EFFECTS),
        "aggregate_epic_status": _next("aggregate_feature_status", effects=JIRA_EFFECTS),
        "aggregate_feature_status": _next("__end__", effects=JIRA_EFFECTS),
        "answer_question": _route(
            "route_after_answer",
            {
                "prd_approval_gate": "prd_approval_gate",
                "spec_approval_gate": "spec_approval_gate",
                "plan_approval_gate": "plan_approval_gate",
                "task_approval_gate": "task_approval_gate",
            },
        ),
        "escalate_blocked": _next("__end__", effects=JIRA_EFFECTS),
    }
    return WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {
                "name": "feature",
                "revision": 1,
                "description": "Forge supported feature golden path",
            },
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "mandatoryPolicies": [POLICY],
                "extensionPoints": ["station-behavior"],
                "steps": steps,
            },
        }
    )


def builtin_definitions() -> tuple[WorkflowDefinition, ...]:
    return (
        builtin_feature_definition(),
        builtin_bug_definition(),
        builtin_task_takeover_definition(),
    )


class FeatureGoldenWorkflow(DeclarativeWorkflow):
    """Default Feature/Story runtime compiled from the published process model."""

    name = "feature"
    description = "Full SDLC workflow compiled from the versioned feature definition"

    def __init__(self) -> None:
        super().__init__(builtin_feature_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in {TicketType.FEATURE, TicketType.STORY}


def builtin_bug_definition() -> WorkflowDefinition:
    """Return the immutable bug-fix golden-path definition."""
    steps = {
        "triage_check": _route(
            "route_current_node",
            {
                "triage_check": "triage_check",
                "triage_gate": "triage_gate",
                "analyze_bug": "analyze_bug",
                "escalate_blocked": "escalate_blocked",
            },
            effects=JIRA_EFFECTS,
        )
        | {"retryBound": 3},
        "triage_gate": _route(
            "route_triage_gate",
            {"triage_check": "triage_check", "__end__": "__end__"},
            kind="gate",
        ),
        "analyze_bug": _route(
            "route_after_analyze_bug",
            {
                "reflect_rca": "reflect_rca",
                "escalate_blocked": "escalate_blocked",
                "__end__": "__end__",
            },
        ),
        "reflect_rca": _route(
            "route_after_reflect_rca",
            {
                "analyze_bug": "analyze_bug",
                "rca_option_gate": "rca_option_gate",
                "escalate_blocked": "escalate_blocked",
                "__end__": "__end__",
            },
        )
        | {"retryBound": 3},
        "rca_option_gate": _route(
            "route_rca_option",
            {
                "plan_bug_fix": "plan_bug_fix",
                "regenerate_rca": "regenerate_rca",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
            effects=JIRA_EFFECTS,
        ),
        "regenerate_rca": _next("analyze_bug", effects=JIRA_EFFECTS),
        "plan_bug_fix": _route(
            "route_after_plan_bug_fix",
            {
                "plan_approval_gate": "plan_approval_gate",
                "plan_bug_fix": "plan_bug_fix",
                "escalate_blocked": "escalate_blocked",
                "__end__": "__end__",
            },
        )
        | {"retryBound": 3},
        "plan_approval_gate": _route(
            "route_plan_approval",
            {
                "decompose_plan": "decompose_plan",
                "regenerate_plan": "regenerate_plan",
                "answer_question": "answer_question",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "regenerate_plan": _route(
            "route_after_regenerate_plan",
            {
                "plan_approval_gate": "plan_approval_gate",
                "regenerate_plan": "regenerate_plan",
                "escalate_blocked": "escalate_blocked",
                "__end__": "__end__",
            },
        )
        | {"retryBound": 3},
        "decompose_plan": _route(
            "route_after_decompose_plan",
            {
                "setup_workspace": "setup_workspace",
                "escalate_blocked": "escalate_blocked",
                "__end__": "__end__",
            },
            effects=JIRA_EFFECTS,
        ),
        "answer_question": _route(
            "route_after_answer",
            {
                "triage_gate": "triage_gate",
                "rca_option_gate": "rca_option_gate",
                "plan_approval_gate": "plan_approval_gate",
            },
            effects=JIRA_EFFECTS,
        ),
        "setup_workspace": _route(
            "route_after_workspace_setup",
            {
                "implement_bug_fix": "implement_bug_fix",
                "escalate_blocked": "escalate_blocked",
            },
        ),
        "implement_bug_fix": _route(
            "route_after_implementation",
            {
                "local_review": "local_review",
                "implement_bug_fix": "implement_bug_fix",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 100},
        "local_review": _route(
            "route_after_local_review",
            {
                "local_review": "local_review",
                "update_documentation": "update_documentation",
                "create_pr": "create_pr",
                "implement_bug_fix": "implement_bug_fix",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 2},
        "update_documentation": _next("create_pr"),
        "create_pr": _route(
            "route_after_pr_creation",
            {
                "teardown_workspace": "teardown_workspace",
                "escalate_blocked": "escalate_blocked",
            },
            effects=SC_EFFECTS,
        ),
        "teardown_workspace": _route(
            "route_after_teardown",
            {"setup_workspace": "setup_workspace", "human_review_gate": "human_review_gate"},
        ),
        "ci_evaluator": _route(
            "route_ci_evaluation",
            {
                "human_review_gate": "human_review_gate",
                "attempt_ci_fix": "attempt_ci_fix",
                "escalate_blocked": "escalate_blocked",
            },
        ),
        "attempt_ci_fix": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "escalate_blocked": "escalate_blocked",
                "ci_evaluator": "ci_evaluator",
                "attempt_ci_fix": "escalate_blocked",
            },
        )
        | {"retryBound": 5},
        "human_review_gate": _route(
            "route_human_review_bug",
            {
                "ci_evaluator": "ci_evaluator",
                "implement_review": "implement_review",
                "post_merge_summary": "post_merge_summary",
                "complete_tasks": "post_merge_summary",
                "__end__": "__end__",
            },
            kind="gate",
            effects=JIRA_EFFECTS,
        ),
        "implement_review": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "review_response_gate": "review_response_gate",
                "implement_review": "implement_review",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 3},
        "review_response_gate": _route(
            "route_review_response",
            {
                "implement_review": "implement_review",
                "human_review_gate": "human_review_gate",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "post_merge_summary": _next("__end__", effects=JIRA_EFFECTS),
        "escalate_blocked": _next("__end__", effects=JIRA_EFFECTS),
    }
    return WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {
                "name": "bug",
                "revision": 1,
                "description": "Forge supported bug-fix golden path",
            },
            "spec": {
                "state": "bug",
                "entry": "triage_check",
                "mandatoryPolicies": [POLICY],
                "extensionPoints": ["station-behavior"],
                "steps": steps,
            },
        }
    )


class BugGoldenWorkflow(DeclarativeWorkflow):
    name = "bug"
    description = "Bug-fix workflow compiled from the versioned process definition"

    def __init__(self) -> None:
        super().__init__(builtin_bug_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type is TicketType.BUG


def builtin_task_takeover_definition() -> WorkflowDefinition:
    """Return the immutable task-takeover golden-path definition."""
    steps = {
        "triage_check": _route(
            "route_after_triage_check",
            {
                "triage_check": "triage_check",
                "triage_gate": "triage_gate",
                "generate_plan": "generate_plan",
                "escalate_blocked": "escalate_blocked",
            },
            effects=JIRA_EFFECTS,
        )
        | {"retryBound": 3},
        "triage_gate": _route(
            "route_triage_gate",
            {"triage_check": "triage_check", "__end__": "__end__"},
            kind="gate",
        ),
        "generate_plan": _route(
            "route_after_generate_plan",
            {
                "generate_plan": "generate_plan",
                "task_plan_approval_gate": "task_plan_approval_gate",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 3},
        "task_plan_approval_gate": _route(
            "route_task_plan_approval",
            {
                "regenerate_plan": "generate_plan",
                "answer_question": "answer_question",
                "setup_workspace": "setup_workspace",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "answer_question": _route(
            "route_after_answer",
            {"task_plan_approval_gate": "task_plan_approval_gate"},
            effects=JIRA_EFFECTS,
        ),
        "setup_workspace": _route(
            "route_after_workspace_setup",
            {
                "execute_task_changes": "execute_task_changes",
                "escalate_blocked": "escalate_blocked",
            },
        ),
        "execute_task_changes": _route(
            "route_after_execution",
            {
                "execute_task_changes": "execute_task_changes",
                "run_qualitative_review": "run_qualitative_review",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 100},
        "run_qualitative_review": _route(
            "route_after_qualitative_review",
            {
                "run_qualitative_review": "run_qualitative_review",
                "execute_task_changes": "execute_task_changes",
                "create_pr": "create_pr",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 3},
        "create_pr": _route(
            "route_after_pr_creation",
            {
                "teardown_workspace": "teardown_workspace",
                "escalate_blocked": "escalate_blocked",
            },
            effects=SC_EFFECTS,
        ),
        "teardown_workspace": _route(
            "route_after_teardown",
            {"setup_workspace": "setup_workspace", "human_review_gate": "human_review_gate"},
        ),
        "ci_evaluator": _route(
            "route_ci_evaluation",
            {
                "human_review_gate": "human_review_gate",
                "attempt_ci_fix": "attempt_ci_fix",
                "escalate_blocked": "escalate_blocked",
            },
        ),
        "attempt_ci_fix": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "escalate_blocked": "escalate_blocked",
                "ci_evaluator": "ci_evaluator",
                "attempt_ci_fix": "escalate_blocked",
            },
        )
        | {"retryBound": 5},
        "human_review_gate": _route(
            "route_human_review_task_takeover",
            {
                "ci_evaluator": "ci_evaluator",
                "implement_review": "implement_review",
                "complete_task_takeover": "complete_task_takeover",
                "complete_tasks": "complete_task_takeover",
                "__end__": "__end__",
            },
            kind="gate",
            effects=JIRA_EFFECTS,
        ),
        "implement_review": _route(
            "route_current_node",
            {
                "human_review_gate": "human_review_gate",
                "review_response_gate": "review_response_gate",
                "implement_review": "implement_review",
                "escalate_blocked": "escalate_blocked",
            },
        )
        | {"retryBound": 3},
        "review_response_gate": _route(
            "route_review_response",
            {
                "implement_review": "implement_review",
                "human_review_gate": "human_review_gate",
                "__end__": "__end__",
            },
            kind="gate",
        ),
        "complete_task_takeover": _next("__end__", effects=JIRA_EFFECTS),
        "escalate_blocked": _next("__end__", effects=JIRA_EFFECTS),
    }
    return WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {
                "name": "task_takeover",
                "revision": 1,
                "description": "Forge supported task-takeover golden path",
            },
            "spec": {
                "state": "task_takeover",
                "entry": "triage_check",
                "mandatoryPolicies": [POLICY],
                "extensionPoints": ["station-behavior"],
                "steps": steps,
            },
        }
    )


class TaskTakeoverGoldenWorkflow(DeclarativeWorkflow):
    name = "task_takeover"
    description = "Task-takeover workflow compiled from the versioned process definition"

    def __init__(self) -> None:
        super().__init__(builtin_task_takeover_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in {TicketType.TASK, TicketType.EPIC} and "forge:managed" in labels
