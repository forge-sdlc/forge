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


def _route(router: str, branches: dict[str, str], *, kind: str = "operation") -> dict[str, Any]:
    return {
        "route": router,
        "branches": branches,
        "kind": kind,
        "requiredPolicies": [POLICY],
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
    return (builtin_feature_definition(),)


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
