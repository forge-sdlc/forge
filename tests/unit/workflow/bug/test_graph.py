"""Unit tests for bug workflow graph structure — new pipeline nodes."""

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.bug.graph import (
    _route_after_answer_bug,
    _route_after_decompose_plan,
    _route_after_local_review,
    _route_after_plan_bug_fix,
    _route_after_reflect_rca,
    _route_after_regenerate_plan,
    _route_after_triage_check,
    _route_human_review_bug,
    build_bug_graph,
    route_entry,
)
from forge.workflow.bug.state import create_initial_bug_state


def _bug_state(**overrides):
    base = {
        "ticket_key": "BUG-1",
        "ticket_type": TicketType.BUG,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "reflection_count": 0,
        "reflection_critique": None,
        "pr_merged": False,
        "local_review_verdict": None,
        "qualitative_retry_count": 0,
    }
    return {**base, **overrides}


class TestRouteEntry:
    """route_entry maps current_node values to correct resume targets."""

    @pytest.mark.parametrize("node,expected", [
        # New nodes
        ("triage_check", "triage_check"),
        ("triage_gate", "triage_gate"),
        ("analyze_bug", "analyze_bug"),
        ("reflect_rca", "reflect_rca"),
        ("rca_option_gate", "rca_option_gate"),
        ("plan_bug_fix", "plan_bug_fix"),
        ("plan_approval_gate", "plan_approval_gate"),
        ("regenerate_plan", "regenerate_plan"),
        ("decompose_plan", "decompose_plan"),
        ("post_merge_summary", "post_merge_summary"),
        # Backward compat: old rca_approval_gate value maps to rca_option_gate
        ("rca_approval_gate", "rca_option_gate"),
        # regenerate_rca performs cleanup before routing through analyze_bug
        ("regenerate_rca", "regenerate_rca"),
        # Preserved existing nodes
        ("setup_workspace", "setup_workspace"),
        ("implement_bug_fix", "implement_bug_fix"),
        ("local_review", "local_review"),
        ("update_documentation", "update_documentation"),
        ("create_pr", "create_pr"),
        ("teardown_workspace", "teardown_workspace"),
        ("ci_evaluator", "ci_evaluator"),
        ("attempt_ci_fix", "ci_evaluator"),
        ("wait_for_ci_gate", "wait_for_ci_gate"),
        ("ai_review", "human_review_gate"),
        ("human_review_gate", "human_review_gate"),
        ("implement_review", "implement_review"),
        ("review_response_gate", "review_response_gate"),
        ("escalate_blocked", "escalate_blocked"),
        ("complete", END),
        ("complete_tasks", END),
        ("aggregate_epic_status", END),
        ("aggregate_feature_status", END),
    ])
    def test_route_entry_mapping(self, node, expected):
        """route_entry maps each current_node to the correct resume target."""
        state = _bug_state(current_node=node)
        assert route_entry(state) == expected

    def test_new_bug_routes_to_triage(self):
        """A fresh bug ticket with no current_node starts at triage_check."""
        state = create_initial_bug_state(ticket_key="BUG-1")
        assert route_entry(state) == "triage_check"

    def test_unknown_node_routes_to_triage(self):
        """An unrecognized current_node value restarts from triage_check."""
        state = _bug_state(current_node="some_unknown_node")
        assert route_entry(state) == "triage_check"

    def test_empty_current_node_routes_to_triage(self):
        """Empty current_node starts at triage_check."""
        state = _bug_state(current_node="")
        assert route_entry(state) == "triage_check"


class TestTriageCheckRouting:
    """_route_after_triage_check proxies triage_check current_node output."""

    def test_routes_retryable_failure_back_to_triage_check(self):
        state = _bug_state(current_node="triage_check", last_error="temporary failure")
        assert _route_after_triage_check(state) == "triage_check"

    def test_routes_to_analyze_bug(self):
        state = _bug_state(current_node="analyze_bug")
        assert _route_after_triage_check(state) == "analyze_bug"

    def test_routes_to_triage_gate(self):
        state = _bug_state(current_node="triage_gate")
        assert _route_after_triage_check(state) == "triage_gate"

    def test_routes_to_escalate_blocked(self):
        state = _bug_state(current_node="escalate_blocked")
        assert _route_after_triage_check(state) == "escalate_blocked"

    def test_unrecognized_value_defaults_to_triage_gate(self):
        """Any unrecognized current_node (e.g. error mid-triage) falls back to triage_gate."""
        state = _bug_state(current_node="something_unexpected")
        assert _route_after_triage_check(state) == "triage_gate"


class TestReflectionLoopRouting:
    """Reflection loop conditional edges route correctly."""

    def test_no_critique_routes_to_rca_gate(self):
        """No reflection_critique (None) routes to rca_option_gate."""
        state = _bug_state(reflection_count=0, reflection_critique=None)
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_empty_critique_routes_to_rca_gate(self):
        """Empty string critique routes to rca_option_gate."""
        state = _bug_state(reflection_count=0, reflection_critique="")
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_critique_under_cap_routes_to_analyze(self):
        """Non-empty critique with reflection_count < 3 routes back to analyze_bug."""
        state = _bug_state(reflection_count=1, reflection_critique="Missing git blame evidence.")
        assert _route_after_reflect_rca(state) == "analyze_bug"

    def test_critique_at_cap_routes_to_gate(self):
        """reflection_count >= 3 routes to rca_option_gate regardless of critique."""
        state = _bug_state(reflection_count=3, reflection_critique="Still incomplete.")
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_critique_above_cap_routes_to_gate(self):
        """reflection_count > 3 also routes to rca_option_gate."""
        state = _bug_state(reflection_count=5, reflection_critique="Still incomplete.")
        assert _route_after_reflect_rca(state) == "rca_option_gate"


class TestMergePath:
    """human_review_gate on merge routes through post_merge_summary."""

    def test_merged_routes_to_post_merge_summary(self):
        """pr_merged=True routes to post_merge_summary, not END."""
        state = _bug_state(pr_merged=True)
        assert _route_human_review_bug(state) == "post_merge_summary"

    def test_not_merged_paused_routes_to_end(self):
        """Not merged, is_paused=True (waiting for review) routes to END."""
        state = _bug_state(pr_merged=False, is_paused=True)
        result = _route_human_review_bug(state)
        assert result == END

    def test_not_merged_fallthrough_does_not_go_to_post_merge(self):
        """Non-merged fallthrough from route_human_review does NOT route to post_merge_summary."""
        # route_human_review falls through to "complete_tasks" when not paused, not merged,
        # and no revision — _route_human_review_bug must NOT intercept this as a merge.
        state = _bug_state(pr_merged=False, is_paused=False)
        result = _route_human_review_bug(state)
        assert result != "post_merge_summary"

    def test_not_merged_implement_review_routes_through(self):
        """Not merged, changes requested (revision_requested=True) routes to implement_review."""
        state = _bug_state(
            pr_merged=False,
            is_paused=False,
            revision_requested=True,
            feedback_comment="Please address these review comments.",
        )
        result = _route_human_review_bug(state)
        assert result == "implement_review"


class TestAnswerQuestionRouting:
    """_route_after_answer_bug returns back to the correct gate."""

    def test_routes_to_rca_option_gate(self):
        """current_node=rca_option_gate routes back to rca_option_gate."""
        state = _bug_state(current_node="rca_option_gate")
        assert _route_after_answer_bug(state) == "rca_option_gate"

    def test_routes_to_triage_gate(self):
        """current_node=triage_gate routes back to triage_gate."""
        state = _bug_state(current_node="triage_gate")
        assert _route_after_answer_bug(state) == "triage_gate"

    def test_routes_to_plan_approval_gate(self):
        """current_node=plan_approval_gate routes back to plan_approval_gate."""
        state = _bug_state(current_node="plan_approval_gate")
        assert _route_after_answer_bug(state) == "plan_approval_gate"

    def test_unknown_falls_back_to_rca_option_gate(self):
        """Unknown current_node falls back to rca_option_gate."""
        state = _bug_state(current_node="some_other_node")
        assert _route_after_answer_bug(state) == "rca_option_gate"


class TestPlanRouting:
    """Planning nodes route based on their returned current_node."""

    def test_plan_bug_fix_success_routes_to_approval(self):
        state = _bug_state(current_node="plan_approval_gate", last_error=None)

        assert _route_after_plan_bug_fix(state) == "plan_approval_gate"

    def test_plan_bug_fix_failure_retries_same_node(self):
        state = _bug_state(current_node="plan_bug_fix", last_error="container failed")

        assert _route_after_plan_bug_fix(state) == "plan_bug_fix"

    def test_plan_bug_fix_retry_cap_routes_to_blocked(self):
        state = _bug_state(
            current_node="plan_bug_fix",
            last_error="container failed",
            retry_count=3,
        )

        assert _route_after_plan_bug_fix(state) == "escalate_blocked"

    def test_regenerate_plan_success_routes_to_approval(self):
        state = _bug_state(current_node="plan_approval_gate", last_error=None)

        assert _route_after_regenerate_plan(state) == "plan_approval_gate"

    def test_regenerate_plan_failure_retries_same_node(self):
        state = _bug_state(current_node="regenerate_plan", last_error="container failed")

        assert _route_after_regenerate_plan(state) == "regenerate_plan"

    def test_regenerate_plan_retry_cap_routes_to_blocked(self):
        state = _bug_state(
            current_node="regenerate_plan",
            last_error="container failed",
            retry_count=3,
        )

        assert _route_after_regenerate_plan(state) == "escalate_blocked"

    def test_decompose_plan_success_routes_to_setup_workspace(self):
        state = _bug_state(current_node="setup_workspace", last_error=None)

        assert _route_after_decompose_plan(state) == "setup_workspace"

    def test_decompose_plan_failure_routes_to_blocked(self):
        state = _bug_state(current_node="decompose_plan", last_error="No repositories found")

        assert _route_after_decompose_plan(state) == "escalate_blocked"


class TestLocalReviewRouting:
    """_route_after_local_review routes based on qualitative verdict."""

    def test_adequate_verdict_routes_to_create_pr(self):
        state = _bug_state(local_review_verdict="adequate", qualitative_retry_count=0)
        assert _route_after_local_review(state) == "update_documentation"

    def test_tests_incomplete_routes_to_implement(self):
        state = _bug_state(local_review_verdict="tests_incomplete", qualitative_retry_count=0)
        assert _route_after_local_review(state) == "implement_bug_fix"

    def test_symptom_only_routes_to_implement(self):
        state = _bug_state(local_review_verdict="symptom_only", qualitative_retry_count=0)
        assert _route_after_local_review(state) == "implement_bug_fix"

    def test_retry_cap_routes_to_update_documentation(self):
        state = _bug_state(local_review_verdict="tests_incomplete", qualitative_retry_count=2)
        assert _route_after_local_review(state) == "update_documentation"

    def test_first_retry_allows_second_attempt(self):
        """qualitative_retry_count=1 (< _QUALITATIVE_CAP=2) still routes back to implement_bug_fix."""
        state = _bug_state(local_review_verdict="tests_incomplete", qualitative_retry_count=1)
        assert _route_after_local_review(state) == "implement_bug_fix"

    def test_retry_at_cap_routes_to_update_documentation(self):
        """qualitative_retry_count=2 (== _QUALITATIVE_CAP) caps the loop and routes to update_documentation."""
        state = _bug_state(local_review_verdict="tests_incomplete", qualitative_retry_count=2)
        assert _route_after_local_review(state) == "update_documentation"

    def test_no_verdict_falls_back_to_current_node(self):
        """No qualitative verdict falls back to current_node (mechanical review path)."""
        state = _bug_state(local_review_verdict=None, current_node="create_pr")
        assert _route_after_local_review(state) == "create_pr"


class TestGraphCompilation:
    """build_bug_graph produces a valid compilable graph."""

    def test_graph_compiles_without_error(self):
        """build_bug_graph() compiles without raising."""
        graph = build_bug_graph()
        compiled = graph.compile()
        assert compiled is not None

    def test_all_new_nodes_present(self):
        """All new pipeline nodes are registered in the graph."""
        graph = build_bug_graph()
        compiled = graph.compile()
        expected_nodes = {
            "triage_check", "triage_gate", "analyze_bug", "reflect_rca",
            "rca_option_gate", "regenerate_rca", "plan_bug_fix",
            "plan_approval_gate", "regenerate_plan", "decompose_plan",
            "post_merge_summary",
        }
        for node in expected_nodes:
            assert node in compiled.nodes, f"Node '{node}' missing from graph"

    def test_post_merge_summary_in_graph(self):
        """post_merge_summary is registered and has edge to END."""
        graph = build_bug_graph()
        compiled = graph.compile()
        assert "post_merge_summary" in compiled.nodes

    def test_ci_fix_routes_through_wait_for_ci_gate(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "attempt_ci_fix"}

        assert "wait_for_ci_gate" in targets

    def test_review_fix_routes_through_wait_for_ci_gate(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "implement_review"}

        assert "wait_for_ci_gate" in targets

    def test_wait_for_ci_gate_paused_routes_to_end(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        ci_gate_edges = compiled.get_graph().edges
        targets = {e.target for e in ci_gate_edges if e.source == "wait_for_ci_gate"}
        assert END in targets

    def test_wait_for_ci_gate_not_paused_routes_to_ci_evaluator(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "wait_for_ci_gate"}
        assert "ci_evaluator" in targets

    def test_attempt_ci_fix_escalates_on_self_referential_failure(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "attempt_ci_fix"}
        assert "escalate_blocked" in targets

    def test_rebase_can_return_to_post_pr_nodes(self):
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "rebase_pr"}

        assert {
            "wait_for_ci_gate",
            "implement_review",
            "review_response_gate",
            "create_pr",
            "teardown_workspace",
        }.issubset(targets)
