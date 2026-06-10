"""Unit tests for PRD approval gate."""

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from forge.workflow.gates import prd_approval_gate, route_prd_approval


class TestPrdApprovalGate:
    """Tests for prd_approval_gate node."""

    @pytest.fixture
    def prd_pending_state(self):
        """State with PRD pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD\n\nGenerated PRD content."
        state["current_node"] = "generate_prd"
        return state

    def test_gate_pauses_workflow(self, prd_pending_state):
        """Gate sets is_paused=True and updates current_node."""
        result = prd_approval_gate(prd_pending_state)

        assert result["is_paused"] is True
        assert result["current_node"] == "prd_approval_gate"

    def test_gate_preserves_prd_content(self, prd_pending_state):
        """Gate preserves existing PRD content."""
        result = prd_approval_gate(prd_pending_state)

        assert result["prd_content"] == "# PRD\n\nGenerated PRD content."


class TestRoutePrdApproval:
    """Tests for route_prd_approval function."""

    @pytest.fixture
    def prd_pending_state(self):
        """State with PRD pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = True
        return state

    def test_routes_to_spec_on_approval(self, prd_pending_state):
        """Approved PRD routes to spec generation when not paused."""
        # When resumed with approval, is_paused is cleared
        prd_pending_state["is_paused"] = False

        result = route_prd_approval(prd_pending_state)

        assert result == "generate_spec"

    def test_routes_to_regenerate_on_rejection(self, prd_pending_state):
        """Rejected PRD with feedback routes to regeneration."""
        prd_pending_state["feedback_comment"] = "Please revise the scope."
        prd_pending_state["revision_requested"] = True

        result = route_prd_approval(prd_pending_state)

        assert result == "regenerate_prd"

    def test_routes_to_end_when_still_paused(self, prd_pending_state):
        """Paused workflow routes to END."""
        # is_paused=True from fixture
        result = route_prd_approval(prd_pending_state)

        assert result == END

    def test_feedback_takes_priority_over_pause(self, prd_pending_state):
        """Revision request routes to regenerate even if paused."""
        prd_pending_state["is_paused"] = True
        prd_pending_state["feedback_comment"] = "Add more detail."
        prd_pending_state["revision_requested"] = True

        result = route_prd_approval(prd_pending_state)

        assert result == "regenerate_prd"


class TestPrdRevisionCycle:
    """Tests for PRD revision cycle."""

    @pytest.fixture
    def state_with_feedback(self):
        """State with rejection feedback."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# Original PRD"
        state["current_node"] = "prd_approval_gate"
        state["feedback_comment"] = "Add user stories section."
        state["revision_requested"] = True
        return state

    def test_revision_routes_to_regenerate(self, state_with_feedback):
        """Revision request routes to regeneration."""
        state_with_feedback["context"] = {
            "labels": ["forge:managed", "forge:prd-pending"],
        }

        result = route_prd_approval(state_with_feedback)

        assert result == "regenerate_prd"

    def test_multiple_revisions_tracked(self, state_with_feedback):
        """Multiple revision cycles can be tracked."""
        state_with_feedback["retry_count"] = 2  # Already revised twice

        # Should still allow regeneration
        state_with_feedback["context"] = {
            "labels": ["forge:managed", "forge:prd-pending"],
        }

        result = route_prd_approval(state_with_feedback)

        assert result == "regenerate_prd"


class TestPrdQuestionRouting:
    """Tests for Q&A routing in PRD approval gate."""

    @pytest.fixture
    def prd_pending_state(self):
        """State with PRD pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["current_node"] = "prd_approval_gate"
        state["is_paused"] = False
        return state

    def test_routes_to_answer_question_when_is_question(self, prd_pending_state):
        """Questions route to answer_question node."""
        prd_pending_state["is_question"] = True
        prd_pending_state["feedback_comment"] = "?Why REST instead of GraphQL?"

        result = route_prd_approval(prd_pending_state)

        assert result == "answer_question"

    def test_question_takes_priority_over_revision(self, prd_pending_state):
        """Question routing takes priority over revision routing."""
        prd_pending_state["is_question"] = True
        prd_pending_state["revision_requested"] = True
        prd_pending_state["feedback_comment"] = "?Why REST?"

        result = route_prd_approval(prd_pending_state)

        assert result == "answer_question"

    def test_routes_to_regenerate_when_feedback_not_question(self, prd_pending_state):
        """Normal feedback routes to regenerate."""
        prd_pending_state["is_question"] = False
        prd_pending_state["revision_requested"] = True
        prd_pending_state["feedback_comment"] = "Add more detail about the API"

        result = route_prd_approval(prd_pending_state)

        assert result == "regenerate_prd"

    def test_question_without_feedback_does_not_route_to_answer(self, prd_pending_state):
        """is_question alone without feedback_comment doesn't route to answer."""
        prd_pending_state["is_question"] = True
        prd_pending_state["feedback_comment"] = ""

        result = route_prd_approval(prd_pending_state)

        # Should proceed to spec generation since not paused
        assert result == "generate_spec"
