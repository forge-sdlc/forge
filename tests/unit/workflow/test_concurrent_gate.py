"""Smoke tests for the concurrent CI/review gate's routing-table shape.

These only exercise routing functions with static state and graph compilation;
they do not interleave a CI event and a review event against shared state. For
a regression test covering the actual concurrent-webhook scenario, see
test_review_arriving_during_in_flight_ci_cycle_is_not_dropped in
tests/unit/orchestrator/test_worker.py.
"""

from langgraph.graph import END


class TestRoutingFunctions:
    def test_no_ci_event_gate_stays_paused(self):
        """Gate with no CI event and no review signal returns END (stays paused)."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            "ticket_key": "TEST-1",
            "is_paused": True,
            "pending_ci_event": False,
            "revision_requested": False,
            "pr_merged": False,
            "feedback_comment": None,
        }
        assert route_human_review(state) == END

    def test_ci_event_routes_through_evaluator(self):
        """CI webhook (pending_ci_event=True) routes to ci_evaluator."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            "ticket_key": "TEST-1",
            "is_paused": False,
            "pending_ci_event": True,
            "revision_requested": False,
            "pr_merged": False,
            "feedback_comment": None,
        }
        assert route_human_review(state) == "ci_evaluator"

    def test_review_event_routes_to_implement_review(self):
        """Review rejection routes to implement_review."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            "ticket_key": "TEST-1",
            "is_paused": False,
            "pending_ci_event": False,
            "revision_requested": True,
            "feedback_comment": "please refactor",
            "pr_merged": False,
        }
        assert route_human_review(state) == "implement_review"

    def test_merge_routes_to_complete_tasks(self):
        """PR merge routes to complete_tasks."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            "ticket_key": "TEST-1",
            "is_paused": False,
            "pending_ci_event": False,
            "revision_requested": False,
            "pr_merged": True,
            "feedback_comment": None,
        }
        assert route_human_review(state) == "complete_tasks"

    def test_external_failure_stays_at_gate(self):
        """external_failure ci_status routes back to human_review_gate (not escalate)."""
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "external_failure"}) == "human_review_gate"

    def test_passed_ci_routes_to_gate_not_end(self):
        """Passed CI routes to human_review_gate (review can now merge)."""
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "passed"}) == "human_review_gate"

    def test_pending_ci_routes_to_gate_not_end(self):
        """Pending CI routes to human_review_gate to re-pause and wait."""
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "pending"}) == "human_review_gate"


class TestGraphCompilation:
    def test_feature_graph_compiles(self):
        """Feature graph builds without error after DRY refactor."""
        from forge.workflow.feature.graph import build_feature_graph

        graph = build_feature_graph()
        compiled = graph.compile()
        assert compiled is not None

    def test_bug_graph_compiles(self):
        """Bug graph builds without error after DRY refactor."""
        from forge.workflow.bug.graph import build_bug_graph

        graph = build_bug_graph()
        compiled = graph.compile()
        assert compiled is not None

    def test_task_takeover_graph_compiles(self):
        """Task takeover graph builds without error after DRY refactor."""
        from forge.workflow.task_takeover.graph import build_task_takeover_graph

        graph = build_task_takeover_graph()
        compiled = graph.compile()
        assert compiled is not None
