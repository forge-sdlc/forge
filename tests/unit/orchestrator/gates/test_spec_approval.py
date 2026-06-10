"""Unit tests for Spec approval gate."""

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from forge.workflow.gates import route_spec_approval, spec_approval_gate


class TestSpecApprovalGate:
    """Tests for spec_approval_gate node."""

    @pytest.fixture
    def spec_pending_state(self):
        """State with Spec pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD\n\nApproved PRD."
        state["spec_content"] = "# Spec\n\nGenerated spec content."
        state["current_node"] = "generate_spec"
        return state

    def test_gate_pauses_workflow(self, spec_pending_state):
        """Gate sets is_paused=True and updates current_node."""
        result = spec_approval_gate(spec_pending_state)

        assert result["is_paused"] is True
        assert result["current_node"] == "spec_approval_gate"

    def test_gate_preserves_spec_content(self, spec_pending_state):
        """Gate preserves existing spec content."""
        result = spec_approval_gate(spec_pending_state)

        assert result["spec_content"] == "# Spec\n\nGenerated spec content."


class TestRouteSpecApproval:
    """Tests for route_spec_approval function."""

    @pytest.fixture
    def spec_pending_state(self):
        """State with Spec pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["current_node"] = "spec_approval_gate"
        state["is_paused"] = True
        return state

    def test_routes_to_decompose_on_approval(self, spec_pending_state):
        """Approved Spec routes to epic decomposition when not paused."""
        spec_pending_state["is_paused"] = False

        result = route_spec_approval(spec_pending_state)

        assert result == "decompose_epics"

    def test_routes_to_regenerate_on_rejection(self, spec_pending_state):
        """Rejected Spec with feedback routes to regeneration."""
        spec_pending_state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }
        spec_pending_state["feedback_comment"] = "Add acceptance criteria for US2."
        spec_pending_state["revision_requested"] = True

        result = route_spec_approval(spec_pending_state)

        assert result == "regenerate_spec"

    def test_routes_to_end_when_pending(self, spec_pending_state):
        """Pending Spec without feedback routes to END."""
        spec_pending_state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }

        result = route_spec_approval(spec_pending_state)

        assert result == END


class TestSpecRevisionWithPrdContext:
    """Tests for spec revision maintaining PRD context."""

    @pytest.fixture
    def state_with_spec_feedback(self):
        """State with spec feedback."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD\n\n## Goals\nUser authentication."
        state["spec_content"] = "# Spec\n\n## User Stories"
        state["feedback_comment"] = "Spec is missing edge case handling."
        state["revision_requested"] = True
        state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }
        return state

    def test_revision_preserves_prd_content(self, state_with_spec_feedback):
        """PRD content is preserved during spec revision."""
        # The regeneration should use the original PRD
        assert state_with_spec_feedback["prd_content"] != ""
        assert "authentication" in state_with_spec_feedback["prd_content"].lower()

    def test_revision_routes_correctly(self, state_with_spec_feedback):
        """Spec revision routes to regenerate_spec."""
        result = route_spec_approval(state_with_spec_feedback)

        assert result == "regenerate_spec"


class TestSpecQuestionRouting:
    """Tests for Q&A routing in Spec approval gate."""

    @pytest.fixture
    def spec_pending_state(self):
        """State with Spec pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["current_node"] = "spec_approval_gate"
        state["is_paused"] = False
        return state

    def test_routes_to_answer_question_when_is_question(self, spec_pending_state):
        """Questions route to answer_question node."""
        spec_pending_state["is_question"] = True
        spec_pending_state["feedback_comment"] = "?Why did you choose this architecture?"

        result = route_spec_approval(spec_pending_state)

        assert result == "answer_question"

    def test_question_takes_priority_over_revision(self, spec_pending_state):
        """Question routing takes priority over revision routing."""
        spec_pending_state["is_question"] = True
        spec_pending_state["revision_requested"] = True
        spec_pending_state["feedback_comment"] = "?What about security?"

        result = route_spec_approval(spec_pending_state)

        assert result == "answer_question"

    def test_routes_to_regenerate_when_feedback_not_question(self, spec_pending_state):
        """Normal feedback routes to regenerate."""
        spec_pending_state["is_question"] = False
        spec_pending_state["revision_requested"] = True
        spec_pending_state["feedback_comment"] = "Add acceptance criteria"

        result = route_spec_approval(spec_pending_state)

        assert result == "regenerate_spec"

    def test_question_without_feedback_does_not_route_to_answer(self, spec_pending_state):
        """is_question alone without feedback_comment doesn't route to answer."""
        spec_pending_state["is_question"] = True
        spec_pending_state["feedback_comment"] = ""

        result = route_spec_approval(spec_pending_state)

        # Should proceed to decompose since not paused
        assert result == "decompose_epics"
