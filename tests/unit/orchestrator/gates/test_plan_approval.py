"""Unit tests for Plan approval gate."""

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from forge.workflow.gates import plan_approval_gate, route_plan_approval


class TestPlanApprovalGate:
    """Tests for plan_approval_gate node."""

    @pytest.fixture
    def plan_pending_state(self):
        """State with Plan pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["epic_keys"] = ["TEST-124", "TEST-125", "TEST-126"]
        state["current_node"] = "decompose_epics"
        return state

    def test_gate_pauses_workflow(self, plan_pending_state):
        """Gate sets is_paused=True and updates current_node."""
        result = plan_approval_gate(plan_pending_state)

        assert result["is_paused"] is True
        assert result["current_node"] == "plan_approval_gate"

    def test_gate_preserves_epic_keys(self, plan_pending_state):
        """Gate preserves existing epic keys."""
        result = plan_approval_gate(plan_pending_state)

        assert result["epic_keys"] == ["TEST-124", "TEST-125", "TEST-126"]


class TestRoutePlanApproval:
    """Tests for route_plan_approval function."""

    @pytest.fixture
    def plan_pending_state(self):
        """State with Plan pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["epic_keys"] = ["TEST-124", "TEST-125"]
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = True
        return state

    def test_routes_to_tasks_on_approval(self, plan_pending_state):
        """Approved Plan routes to task generation when not paused."""
        plan_pending_state["is_paused"] = False

        result = route_plan_approval(plan_pending_state)

        assert result == "generate_tasks"

    def test_routes_to_regenerate_all_on_full_rejection(self, plan_pending_state):
        """Full plan rejection routes to regenerate all epics."""
        plan_pending_state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "feature",  # Full feature-level rejection
        }
        plan_pending_state["feedback_comment"] = "The epic breakdown doesn't make sense."
        plan_pending_state["revision_requested"] = True

        result = route_plan_approval(plan_pending_state)

        assert result == "regenerate_all_epics"

    def test_routes_to_update_single_on_epic_rejection(self, plan_pending_state):
        """Single epic rejection routes to update that epic."""
        plan_pending_state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-125",
        }
        plan_pending_state["current_epic_key"] = "TEST-125"
        plan_pending_state["feedback_comment"] = "Epic 2 needs more detail."
        plan_pending_state["revision_requested"] = True

        result = route_plan_approval(plan_pending_state)

        assert result == "update_single_epic"

    def test_routes_to_end_when_pending(self, plan_pending_state):
        """Pending Plan without feedback routes to END."""
        plan_pending_state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
        }

        result = route_plan_approval(plan_pending_state)

        assert result == END


class TestPlanRevisionScenarios:
    """Tests for different plan revision scenarios."""

    @pytest.fixture
    def state_with_epics(self):
        """State with multiple epics."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["epic_keys"] = ["TEST-124", "TEST-125", "TEST-126"]
        return state

    def test_full_regen_deletes_all_epics(self, state_with_epics):
        """Full regeneration affects all epics."""
        state_with_epics["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "feature",
        }
        state_with_epics["feedback_comment"] = "Start over with a different approach."
        state_with_epics["revision_requested"] = True

        result = route_plan_approval(state_with_epics)

        assert result == "regenerate_all_epics"

    def test_single_epic_update_preserves_others(self, state_with_epics):
        """Single epic update preserves other epics."""
        state_with_epics["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-125",
        }
        state_with_epics["current_epic_key"] = "TEST-125"
        state_with_epics["feedback_comment"] = "Just fix this one epic."
        state_with_epics["revision_requested"] = True

        result = route_plan_approval(state_with_epics)

        assert result == "update_single_epic"
        # Other epics should remain in state
        assert "TEST-124" in state_with_epics["epic_keys"]
        assert "TEST-126" in state_with_epics["epic_keys"]

    def test_partial_approval_scenario(self, state_with_epics):
        """Some epics approved, one needs revision."""
        # This tests the scenario where user approves some epics
        # but requests changes to one specific epic
        state_with_epics["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-126",
            "approved_epics": ["TEST-124", "TEST-125"],
        }
        state_with_epics["current_epic_key"] = "TEST-126"
        state_with_epics["feedback_comment"] = "Epic 3 scope is too broad."
        state_with_epics["revision_requested"] = True

        result = route_plan_approval(state_with_epics)

        assert result == "update_single_epic"


class TestPlanQuestionRouting:
    """Tests for Q&A routing in Plan approval gate."""

    @pytest.fixture
    def plan_pending_state(self):
        """State with Plan pending."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["epic_keys"] = ["TEST-124", "TEST-125"]
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = False
        return state

    def test_routes_to_answer_question_when_is_question(self, plan_pending_state):
        """Questions route to answer_question node."""
        plan_pending_state["is_question"] = True
        plan_pending_state["feedback_comment"] = "?Why split into two epics?"

        result = route_plan_approval(plan_pending_state)

        assert result == "answer_question"

    def test_question_takes_priority_over_revision(self, plan_pending_state):
        """Question routing takes priority over revision routing."""
        plan_pending_state["is_question"] = True
        plan_pending_state["revision_requested"] = True
        plan_pending_state["feedback_comment"] = "?What's the dependency order?"

        result = route_plan_approval(plan_pending_state)

        assert result == "answer_question"

    def test_routes_to_regenerate_all_when_feedback_not_question(self, plan_pending_state):
        """Normal feedback routes to regenerate all epics."""
        plan_pending_state["is_question"] = False
        plan_pending_state["revision_requested"] = True
        plan_pending_state["feedback_comment"] = "Rethink the epic breakdown"

        result = route_plan_approval(plan_pending_state)

        assert result == "regenerate_all_epics"

    def test_question_without_feedback_does_not_route_to_answer(self, plan_pending_state):
        """is_question alone without feedback_comment doesn't route to answer."""
        plan_pending_state["is_question"] = True
        plan_pending_state["feedback_comment"] = ""

        result = route_plan_approval(plan_pending_state)

        # Should proceed to generate_tasks since not paused
        assert result == "generate_tasks"
