"""Tests for the implement_review node and review_response_gate (proposal 007)."""

from langgraph.graph import END
from tests.fixtures.workflow_states import make_workflow_state

# ── State fields ──────────────────────────────────────────────────────────────


class TestReviewStateFields:

    def test_review_comments_in_review_integration_state(self):
        """review_comments must be a field in ReviewIntegrationState."""
        from forge.workflow.base import ReviewIntegrationState
        assert "review_comments" in ReviewIntegrationState.__annotations__

    def test_contested_comments_in_review_integration_state(self):
        from forge.workflow.base import ReviewIntegrationState
        assert "contested_comments" in ReviewIntegrationState.__annotations__

    def test_review_response_posted_in_review_integration_state(self):
        from forge.workflow.base import ReviewIntegrationState
        assert "review_response_posted" in ReviewIntegrationState.__annotations__

    def test_initial_feature_state_has_empty_review_fields(self):
        from forge.models.workflow import TicketType
        from forge.workflow.feature.state import create_initial_feature_state
        state = create_initial_feature_state(
            thread_id="t", ticket_key="TEST-1", ticket_type=TicketType.FEATURE
        )
        assert state.get("review_comments") == []
        assert state.get("contested_comments") == []
        assert state.get("review_response_posted") is False


# ── route_human_review routes to implement_review on changes_requested ────────


class TestHumanReviewRoutingToImplementReview:

    def test_changes_requested_routes_to_implement_review_not_implement_task(self):
        """On changes_requested, route to implement_review, not implement_task."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            revision_requested=True,
            feedback_comment="The session token must be HMAC-signed.",
        )
        assert route_human_review(state) == "implement_review"

    def test_merged_still_routes_to_complete_tasks(self):
        """PR merged still goes to complete_tasks."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            pr_merged=True,
        )
        assert route_human_review(state) == "complete_tasks"

    def test_paused_still_routes_to_end(self):
        """Waiting for review still returns END."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=True,
        )
        assert route_human_review(state) == END


# ── review_response_gate pause node ──────────────────────────────────────────


class TestReviewResponseGate:

    def test_review_response_gate_pauses_workflow(self):
        """review_response_gate sets is_paused=True."""
        from forge.workflow.nodes.implement_review import review_response_gate

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
        )
        result = review_response_gate(state)
        assert result["is_paused"] is True
        assert result["current_node"] == "review_response_gate"

    def test_route_review_response_confirmed_resumes_implement_review(self):
        """When human confirms, route back to implement_review.

        Worker sets revision_requested=True (confirmed) and clears
        contested_comments so the agent knows to implement this time.
        """
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            revision_requested=True,   # human confirmed — implement it
            contested_comments=[],     # cleared by worker
        )
        assert route_review_response(state) == "implement_review"

    def test_route_review_response_withdrawn_routes_to_human_review_gate(self):
        """When human withdraws the request, route back to human_review_gate."""
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            revision_requested=False,
            pr_merged=False,
            feedback_comment=None,
        )
        # No revision_requested and no contested → human withdrew
        assert route_review_response(state) == "human_review_gate"

    def test_route_review_response_paused_returns_end(self):
        """Still waiting for human response → END."""
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=True,
        )
        assert route_review_response(state) == END


# ── implement_review in feature graph ────────────────────────────────────────


class TestImplementReviewInFeatureGraph:

    def test_implement_review_is_a_node(self):
        """implement_review must be a node in the feature graph."""
        from forge.workflow.feature.graph import build_feature_graph
        graph = build_feature_graph()
        compiled = graph.compile()
        assert "implement_review" in compiled.get_graph().nodes

    def test_review_response_gate_is_a_node(self):
        """review_response_gate must be a node in the feature graph."""
        from forge.workflow.feature.graph import build_feature_graph
        graph = build_feature_graph()
        compiled = graph.compile()
        assert "review_response_gate" in compiled.get_graph().nodes

    def test_human_review_gate_has_implement_review_edge(self):
        """human_review_gate must have an edge to implement_review."""
        from forge.workflow.feature.graph import build_feature_graph
        graph = build_feature_graph()
        compiled = graph.compile()
        targets = {
            e.target for e in compiled.get_graph().edges
            if e.source == "human_review_gate"
        }
        assert "implement_review" in targets

    def test_implement_task_not_reachable_from_human_review_gate(self):
        """implement_task must NOT be a direct target of human_review_gate."""
        from forge.workflow.feature.graph import build_feature_graph
        graph = build_feature_graph()
        compiled = graph.compile()
        targets = {
            e.target for e in compiled.get_graph().edges
            if e.source == "human_review_gate"
        }
        assert "implement_task" not in targets


# ── implement_review in bug graph ────────────────────────────────────────────


class TestImplementReviewInBugGraph:

    def test_implement_review_is_a_node_in_bug_graph(self):
        from forge.workflow.bug.graph import build_bug_graph
        graph = build_bug_graph()
        compiled = graph.compile()
        assert "implement_review" in compiled.get_graph().nodes

    def test_human_review_gate_routes_to_implement_review_in_bug_graph(self):
        from forge.workflow.bug.graph import build_bug_graph
        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {
            e.target for e in compiled.get_graph().edges
            if e.source == "human_review_gate"
        }
        assert "implement_review" in targets


# ── resume routing ────────────────────────────────────────────────────────────


class TestResumeRoutingForReviewNodes:

    def test_feature_resumes_at_implement_review(self):
        from forge.workflow.feature.graph import route_by_ticket_type
        state = make_workflow_state(current_node="implement_review")
        assert route_by_ticket_type(state) == "implement_review"

    def test_feature_resumes_at_review_response_gate(self):
        from forge.workflow.feature.graph import route_by_ticket_type
        state = make_workflow_state(current_node="review_response_gate")
        assert route_by_ticket_type(state) == "review_response_gate"

    def test_bug_resumes_at_implement_review(self):
        from forge.workflow.bug.graph import route_entry
        state = make_workflow_state(current_node="implement_review")
        assert route_entry(state) == "implement_review"

    def test_bug_resumes_at_review_response_gate(self):
        from forge.workflow.bug.graph import route_entry
        state = make_workflow_state(current_node="review_response_gate")
        assert route_entry(state) == "review_response_gate"
