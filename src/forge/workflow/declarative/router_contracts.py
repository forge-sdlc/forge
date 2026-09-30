"""Reviewed output contracts for registered static routers by source node."""

ROUTER_CONTRACTS: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "bug": {
        "analyze_bug": (
            "route_after_analyze_bug",
            frozenset(["__end__", "escalate_blocked", "reflect_rca"]),
        ),
        "answer_question": (
            "route_after_answer",
            frozenset(["plan_approval_gate", "rca_option_gate", "triage_gate"]),
        ),
        "attempt_ci_fix": (
            "route_current_node",
            frozenset(["attempt_ci_fix", "ci_evaluator", "escalate_blocked", "human_review_gate"]),
        ),
        "ci_evaluator": (
            "route_ci_evaluation",
            frozenset(["attempt_ci_fix", "escalate_blocked", "human_review_gate"]),
        ),
        "create_pr": (
            "route_after_pr_creation",
            frozenset(["escalate_blocked", "teardown_workspace"]),
        ),
        "decompose_plan": (
            "route_after_decompose_plan",
            frozenset(["__end__", "escalate_blocked", "setup_workspace"]),
        ),
        "human_review_gate": (
            "route_human_review_bug",
            frozenset(
                [
                    "__end__",
                    "ci_evaluator",
                    "complete_tasks",
                    "implement_review",
                    "post_merge_summary",
                ]
            ),
        ),
        "implement_review": (
            "route_current_node",
            frozenset(
                [
                    "escalate_blocked",
                    "human_review_gate",
                    "implement_review",
                    "review_response_gate",
                ]
            ),
        ),
        "implement_work": (
            "route_after_implementation",
            frozenset(["escalate_blocked", "implement_work", "local_review"]),
        ),
        "local_review": (
            "route_after_local_review",
            frozenset(
                [
                    "create_pr",
                    "escalate_blocked",
                    "implement_work",
                    "local_review",
                    "update_documentation",
                ]
            ),
        ),
        "plan_approval_gate": (
            "route_plan_approval",
            frozenset(["__end__", "answer_question", "decompose_plan", "regenerate_plan"]),
        ),
        "plan_bug_fix": (
            "route_after_plan_bug_fix",
            frozenset(["__end__", "escalate_blocked", "plan_approval_gate", "plan_bug_fix"]),
        ),
        "rca_option_gate": (
            "route_rca_option",
            frozenset(["__end__", "answer_question", "plan_bug_fix", "regenerate_rca"]),
        ),
        "reflect_rca": (
            "route_after_reflect_rca",
            frozenset(["__end__", "analyze_bug", "escalate_blocked", "rca_option_gate"]),
        ),
        "regenerate_plan": (
            "route_after_regenerate_plan",
            frozenset(["__end__", "escalate_blocked", "plan_approval_gate", "regenerate_plan"]),
        ),
        "review_response_gate": (
            "route_review_response",
            frozenset(["__end__", "human_review_gate", "implement_review"]),
        ),
        "setup_workspace": (
            "route_after_workspace_setup",
            frozenset(["escalate_blocked", "implement_work"]),
        ),
        "teardown_workspace": (
            "route_after_teardown",
            frozenset(["human_review_gate", "setup_workspace"]),
        ),
        "triage_check": (
            "route_current_node",
            frozenset(["analyze_bug", "escalate_blocked", "triage_check", "triage_gate"]),
        ),
        "triage_gate": ("route_triage_gate", frozenset(["__end__", "triage_check"])),
    },
    "feature": {
        "answer_question": (
            "route_after_answer",
            frozenset(
                [
                    "plan_approval_gate",
                    "prd_approval_gate",
                    "spec_approval_gate",
                    "task_approval_gate",
                ]
            ),
        ),
        "attempt_ci_fix": (
            "route_current_node",
            frozenset(["attempt_ci_fix", "ci_evaluator", "escalate_blocked", "human_review_gate"]),
        ),
        "ci_evaluator": (
            "route_ci_evaluation",
            frozenset(["attempt_ci_fix", "escalate_blocked", "human_review_gate"]),
        ),
        "create_pr": (
            "route_after_pr_creation",
            frozenset(["escalate_blocked", "teardown_workspace"]),
        ),
        "decompose_epics": (
            "route_after_epic_decomposition",
            frozenset(["__end__", "plan_approval_gate"]),
        ),
        "generate_prd": ("route_after_generation", frozenset(["__end__", "prd_approval_gate"])),
        "generate_spec": (
            "route_after_spec_generation",
            frozenset(["__end__", "spec_approval_gate"]),
        ),
        "generate_tasks": (
            "route_after_task_generation",
            frozenset(["__end__", "task_approval_gate"]),
        ),
        "human_review_gate": (
            "route_human_review",
            frozenset(["__end__", "ci_evaluator", "complete_tasks", "implement_review"]),
        ),
        "implement_review": (
            "route_current_node",
            frozenset(
                [
                    "escalate_blocked",
                    "human_review_gate",
                    "implement_review",
                    "review_response_gate",
                ]
            ),
        ),
        "implement_work": (
            "route_implementation",
            frozenset(["escalate_blocked", "implement_work", "local_review"]),
        ),
        "local_review": (
            "route_current_node",
            frozenset(["create_pr", "escalate_blocked", "local_review"]),
        ),
        "plan_approval_gate": (
            "route_plan_approval",
            frozenset(
                [
                    "__end__",
                    "answer_question",
                    "generate_tasks",
                    "provision_epics",
                    "regenerate_all_epics",
                    "update_single_epic",
                ]
            ),
        ),
        "prd_approval_gate": (
            "route_prd_approval",
            frozenset(["__end__", "answer_question", "generate_spec", "regenerate_prd"]),
        ),
        "regenerate_all_epics": (
            "route_after_epic_regeneration",
            frozenset(["__end__", "plan_approval_gate"]),
        ),
        "regenerate_all_tasks": (
            "route_after_task_regeneration",
            frozenset(["__end__", "task_approval_gate"]),
        ),
        "regenerate_epic_tasks": (
            "route_after_epic_task_regeneration",
            frozenset(["__end__", "task_approval_gate"]),
        ),
        "regenerate_prd": (
            "route_after_prd_regeneration",
            frozenset(["__end__", "prd_approval_gate"]),
        ),
        "regenerate_spec": (
            "route_after_spec_regeneration",
            frozenset(["__end__", "spec_approval_gate"]),
        ),
        "review_response_gate": (
            "route_review_response",
            frozenset(["__end__", "human_review_gate", "implement_review"]),
        ),
        "setup_workspace": (
            "route_after_workspace_setup",
            frozenset(["escalate_blocked", "implement_work"]),
        ),
        "spec_approval_gate": (
            "route_spec_approval",
            frozenset(["__end__", "answer_question", "decompose_epics", "regenerate_spec"]),
        ),
        "task_approval_gate": (
            "route_task_approval",
            frozenset(
                [
                    "__end__",
                    "answer_question",
                    "provision_tasks",
                    "regenerate_all_tasks",
                    "regenerate_epic_tasks",
                    "task_router",
                    "update_single_task",
                ]
            ),
        ),
        "teardown_workspace": (
            "route_after_teardown",
            frozenset(["human_review_gate", "setup_workspace"]),
        ),
        "update_single_epic": (
            "route_after_single_epic_update",
            frozenset(["__end__", "plan_approval_gate"]),
        ),
        "update_single_task": (
            "route_after_single_task_update",
            frozenset(["__end__", "task_approval_gate"]),
        ),
    },
    "task_takeover": {
        "answer_question": ("route_after_answer", frozenset(["task_plan_approval_gate"])),
        "attempt_ci_fix": (
            "route_current_node",
            frozenset(["attempt_ci_fix", "ci_evaluator", "escalate_blocked", "human_review_gate"]),
        ),
        "ci_evaluator": (
            "route_ci_evaluation",
            frozenset(["attempt_ci_fix", "escalate_blocked", "human_review_gate"]),
        ),
        "create_pr": (
            "route_after_pr_creation",
            frozenset(["escalate_blocked", "teardown_workspace"]),
        ),
        "generate_plan": (
            "route_after_generate_plan",
            frozenset(["escalate_blocked", "generate_plan", "task_plan_approval_gate"]),
        ),
        "human_review_gate": (
            "route_human_review_task_takeover",
            frozenset(
                [
                    "__end__",
                    "ci_evaluator",
                    "complete_task_takeover",
                    "complete_tasks",
                    "implement_review",
                ]
            ),
        ),
        "implement_review": (
            "route_current_node",
            frozenset(
                [
                    "escalate_blocked",
                    "human_review_gate",
                    "implement_review",
                    "review_response_gate",
                ]
            ),
        ),
        "implement_work": (
            "route_after_execution",
            frozenset(["escalate_blocked", "implement_work", "run_qualitative_review"]),
        ),
        "review_response_gate": (
            "route_review_response",
            frozenset(["__end__", "human_review_gate", "implement_review"]),
        ),
        "run_qualitative_review": (
            "route_after_qualitative_review",
            frozenset(
                ["create_pr", "escalate_blocked", "implement_work", "run_qualitative_review"]
            ),
        ),
        "setup_workspace": (
            "route_after_workspace_setup",
            frozenset(["escalate_blocked", "implement_work"]),
        ),
        "task_plan_approval_gate": (
            "route_task_plan_approval",
            frozenset(["__end__", "answer_question", "regenerate_plan", "setup_workspace"]),
        ),
        "teardown_workspace": (
            "route_after_teardown",
            frozenset(["human_review_gate", "setup_workspace"]),
        ),
        "triage_check": (
            "route_after_triage_check",
            frozenset(["escalate_blocked", "generate_plan", "triage_check", "triage_gate"]),
        ),
        "triage_gate": ("route_triage_gate", frozenset(["__end__", "triage_check"])),
    },
}


def validate_router_outcomes(
    state: str, node_name: str, router_name: str, outcomes: set[str]
) -> None:
    """Require every static router result, independent of authored destinations."""
    contract = ROUTER_CONTRACTS[state].get(node_name)
    if contract is None or contract[0] != router_name:
        raise ValueError(
            f"router '{router_name}' has no reviewed outcome contract on '{node_name}'"
        )
    expected = set(contract[1])
    missing = expected - outcomes
    if missing:
        raise ValueError(f"step '{node_name}' omits router outcome '{sorted(missing)[0]}'")
    extra = outcomes - expected
    if extra:
        raise ValueError(
            f"step '{node_name}' adds unregistered router outcome '{sorted(extra)[0]}'"
        )
