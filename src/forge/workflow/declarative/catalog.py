"""Allowlisted Forge nodes, routers, and state profiles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from forge.workflow.node_contracts import contracts_for
from forge.workflow.preconditions import NodeContract


@dataclass(frozen=True)
class StateProfile:
    schema: type
    initializer: Callable[..., Any]
    nodes: dict[str, Any]
    routers: dict[str, Any]
    pause_nodes: frozenset[str]
    contracts: dict[str, NodeContract] = field(default_factory=dict)
    station_bindings: dict[str, tuple[str, str]] = field(default_factory=dict)


def _common_nodes() -> dict[str, Callable[..., Any]]:
    from forge.workflow.nodes import (
        attempt_ci_fix,
        create_pull_request,
        escalate_to_blocked,
        evaluate_ci_status,
        human_review_gate,
        implement_review,
        implement_work,
        rebase_pr,
        review_response_gate,
        setup_workspace,
        teardown_and_route,
        update_documentation,
    )

    return {
        "attempt_ci_fix": attempt_ci_fix,
        "ci_evaluator": evaluate_ci_status,
        "create_pr": create_pull_request,
        "escalate_blocked": escalate_to_blocked,
        "human_review_gate": human_review_gate,
        "implement_work": implement_work,
        "implement_review": implement_review,
        "rebase_pr": rebase_pr,
        "review_response_gate": review_response_gate,
        "setup_workspace": setup_workspace,
        "teardown_workspace": teardown_and_route,
        "update_documentation": update_documentation,
    }


def _common_routers() -> dict[str, Callable[..., Any]]:
    from forge.workflow.nodes import route_human_review, route_review_response
    from forge.workflow.post_pr import _route_after_teardown, _route_ci_evaluation

    return {
        "route_after_teardown": _route_after_teardown,
        "route_ci_evaluation": _route_ci_evaluation,
        "route_human_review": route_human_review,
        "route_review_response": route_review_response,
        "route_current_node": lambda state: state.get("current_node", "__end__"),
    }


def get_state_profile(name: str) -> StateProfile:
    """Return a fresh profile catalog without accepting dynamic import paths."""
    common_nodes = _common_nodes()
    common_routers = _common_routers()
    common_pauses = frozenset({"human_review_gate", "review_response_gate", "ci_evaluator"})

    if name == "feature":
        from forge.workflow.feature.graph import (
            _route_after_answer,
            _route_after_epic_decomposition,
            _route_after_epic_regeneration,
            _route_after_epic_task_regeneration,
            _route_after_generation,
            _route_after_prd_regeneration,
            _route_after_single_epic_update,
            _route_after_single_task_update,
            _route_after_spec_generation,
            _route_after_spec_regeneration,
            _route_after_task_generation,
            _route_after_task_regeneration,
            _route_after_workspace_setup,
            _route_implementation,
        )
        from forge.workflow.feature.state import FeatureState, create_initial_feature_state
        from forge.workflow.gates import (
            plan_approval_gate,
            prd_approval_gate,
            route_plan_approval,
            route_prd_approval,
            route_spec_approval,
            route_task_approval,
            spec_approval_gate,
            task_approval_gate,
        )
        from forge.workflow.nodes import (
            aggregate_epic_status,
            aggregate_feature_status,
            answer_question,
            complete_tasks,
            decompose_epics,
            generate_prd,
            generate_spec,
            generate_tasks,
            implement_task,
            local_review_changes,
            regenerate_all_epics,
            regenerate_prd_with_feedback,
            regenerate_spec_with_feedback,
            route_tasks_by_repo,
            update_single_epic,
        )
        from forge.workflow.nodes.task_generation import (
            regenerate_all_tasks,
            regenerate_epic_tasks,
            update_single_task,
        )
        from forge.workflow.nodes.task_router import route_tasks_parallel
        from forge.workflow.post_pr import route_after_pr_creation

        nodes: dict[str, Any] = {
            **common_nodes,
            "aggregate_epic_status": aggregate_epic_status,
            "aggregate_feature_status": aggregate_feature_status,
            "answer_question": answer_question,
            "complete_tasks": complete_tasks,
            "decompose_epics": decompose_epics,
            "generate_prd": generate_prd,
            "generate_spec": generate_spec,
            "generate_tasks": generate_tasks,
            "implement_task": implement_task,
            "local_review": local_review_changes,
            "plan_approval_gate": plan_approval_gate,
            "prd_approval_gate": prd_approval_gate,
            "regenerate_all_epics": regenerate_all_epics,
            "regenerate_all_tasks": regenerate_all_tasks,
            "regenerate_epic_tasks": regenerate_epic_tasks,
            "regenerate_prd": regenerate_prd_with_feedback,
            "regenerate_spec": regenerate_spec_with_feedback,
            "spec_approval_gate": spec_approval_gate,
            "task_approval_gate": task_approval_gate,
            "task_router": route_tasks_by_repo,
            "update_single_epic": update_single_epic,
            "update_single_task": update_single_task,
        }
        routers: dict[str, Any] = {
            **common_routers,
            "route_after_answer": _route_after_answer,
            "route_after_epic_decomposition": _route_after_epic_decomposition,
            "route_after_epic_regeneration": _route_after_epic_regeneration,
            "route_after_epic_task_regeneration": _route_after_epic_task_regeneration,
            "route_after_generation": _route_after_generation,
            "route_after_pr_creation": route_after_pr_creation,
            "route_after_prd_regeneration": _route_after_prd_regeneration,
            "route_after_single_epic_update": _route_after_single_epic_update,
            "route_after_single_task_update": _route_after_single_task_update,
            "route_after_spec_generation": _route_after_spec_generation,
            "route_after_spec_regeneration": _route_after_spec_regeneration,
            "route_after_task_generation": _route_after_task_generation,
            "route_after_task_regeneration": _route_after_task_regeneration,
            "route_after_workspace_setup": _route_after_workspace_setup,
            "route_implementation": _route_implementation,
            "route_plan_approval": route_plan_approval,
            "route_prd_approval": route_prd_approval,
            "route_spec_approval": route_spec_approval,
            "route_task_approval": route_task_approval,
            "route_tasks_parallel": route_tasks_parallel,
        }
        pauses = common_pauses | {
            "plan_approval_gate",
            "prd_approval_gate",
            "spec_approval_gate",
            "task_approval_gate",
        }
        return StateProfile(
            FeatureState,
            create_initial_feature_state,
            nodes,
            routers,
            pauses,
            contracts_for(nodes),
            {
                "task_router": ("task-routing", "1.0"),
                "prd_approval_gate": ("approval-policy", "1.0"),
                "spec_approval_gate": ("approval-policy", "1.0"),
                "plan_approval_gate": ("approval-policy", "1.0"),
                "task_approval_gate": ("approval-policy", "1.0"),
            },
        )

    if name == "bug":
        from forge.workflow.bug.graph import (
            _answer_question_bug,
            _implement_task_bug,
            _local_review_bug,
            _route_after_analyze_bug,
            _route_after_answer_bug,
            _route_after_decompose_plan,
            _route_after_implementation,
            _route_after_local_review,
            _route_after_plan_bug_fix,
            _route_after_reflect_rca,
            _route_after_regenerate_plan,
            _route_after_workspace_setup,
            _route_human_review_bug,
        )
        from forge.workflow.bug.state import BugState, create_initial_bug_state
        from forge.workflow.nodes import (
            analyze_bug,
            decompose_plan,
            plan_bug_fix,
            post_merge_summary,
            rca_option_gate,
            reflect_rca,
            regenerate_plan,
            regenerate_rca,
            route_rca_option,
            route_triage_gate,
            triage_check,
            triage_gate,
        )
        from forge.workflow.nodes import (
            route_plan_approval as route_bug_plan_approval,
        )
        from forge.workflow.nodes.plan_bug_fix import (
            plan_approval_gate as bug_plan_approval_gate,
        )
        from forge.workflow.post_pr import route_after_pr_creation

        nodes = {
            **common_nodes,
            "analyze_bug": analyze_bug,
            "answer_question": _answer_question_bug,
            "decompose_plan": decompose_plan,
            "implement_bug_fix": _implement_task_bug,
            "local_review": _local_review_bug,
            "plan_approval_gate": bug_plan_approval_gate,
            "plan_bug_fix": plan_bug_fix,
            "post_merge_summary": post_merge_summary,
            "rca_option_gate": rca_option_gate,
            "reflect_rca": reflect_rca,
            "regenerate_plan": regenerate_plan,
            "regenerate_rca": regenerate_rca,
            "triage_check": triage_check,
            "triage_gate": triage_gate,
        }
        routers = {
            **common_routers,
            "route_after_analyze_bug": _route_after_analyze_bug,
            "route_after_answer": _route_after_answer_bug,
            "route_after_decompose_plan": _route_after_decompose_plan,
            "route_after_implementation": _route_after_implementation,
            "route_after_local_review": _route_after_local_review,
            "route_after_plan_bug_fix": _route_after_plan_bug_fix,
            "route_after_pr_creation": route_after_pr_creation,
            "route_after_reflect_rca": _route_after_reflect_rca,
            "route_after_regenerate_plan": _route_after_regenerate_plan,
            "route_after_workspace_setup": _route_after_workspace_setup,
            "route_plan_approval": route_bug_plan_approval,
            "route_rca_option": route_rca_option,
            "route_triage_gate": route_triage_gate,
            "route_human_review_bug": _route_human_review_bug,
        }
        pauses = common_pauses | {"triage_gate", "rca_option_gate", "plan_approval_gate"}
        return StateProfile(
            BugState,
            create_initial_bug_state,
            nodes,
            routers,
            pauses,
            contracts_for(nodes),
            {"plan_approval_gate": ("approval-policy", "1.0")},
        )

    if name == "task_takeover":
        from forge.workflow.gates import route_task_plan_approval, task_plan_approval_gate
        from forge.workflow.nodes import (
            answer_question,
            execute_task_changes,
            generate_plan,
            route_triage_gate,
            run_qualitative_review,
            triage_gate,
            triage_task,
        )
        from forge.workflow.post_pr import route_after_pr_creation
        from forge.workflow.task_takeover.graph import (
            _route_after_answer as route_after_task_answer,
        )
        from forge.workflow.task_takeover.graph import (
            _route_after_execution,
            _route_after_generate_plan,
            _route_after_qualitative_review,
            _route_after_triage_check,
            _route_human_review_task_takeover,
            complete_task_takeover,
        )
        from forge.workflow.task_takeover.graph import (
            _route_after_workspace_setup as route_after_task_workspace_setup,
        )
        from forge.workflow.task_takeover.state import (
            TaskTakeoverState,
            create_initial_task_takeover_state,
        )

        nodes = {
            **common_nodes,
            "answer_question": answer_question,
            "complete_task_takeover": complete_task_takeover,
            "execute_task_changes": execute_task_changes,
            "generate_plan": generate_plan,
            "run_qualitative_review": run_qualitative_review,
            "task_plan_approval_gate": task_plan_approval_gate,
            "triage_check": triage_task,
            "triage_gate": triage_gate,
        }
        routers = {
            **common_routers,
            "route_after_answer": route_after_task_answer,
            "route_after_execution": _route_after_execution,
            "route_after_generate_plan": _route_after_generate_plan,
            "route_after_pr_creation": route_after_pr_creation,
            "route_after_qualitative_review": _route_after_qualitative_review,
            "route_after_triage_check": _route_after_triage_check,
            "route_after_workspace_setup": route_after_task_workspace_setup,
            "route_task_plan_approval": route_task_plan_approval,
            "route_triage_gate": route_triage_gate,
            "route_human_review_task_takeover": _route_human_review_task_takeover,
        }
        pauses = common_pauses | {"triage_gate", "task_plan_approval_gate"}
        return StateProfile(
            TaskTakeoverState,
            create_initial_task_takeover_state,
            nodes,
            routers,
            pauses,
            contracts_for(nodes),
            {"task_plan_approval_gate": ("approval-policy", "1.0")},
        )

    raise ValueError(f"unknown state profile: {name}")
