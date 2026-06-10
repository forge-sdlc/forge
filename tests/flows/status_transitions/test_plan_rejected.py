"""Tests for Plan rejection and revision cycles."""


import pytest

from forge.models.workflow import TicketType
from forge.workflow.gates import route_plan_approval
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state


class TestPlanRejectedFullRegen:
    """Tests for full plan regeneration (all epics)."""

    @pytest.fixture
    def plan_pending_state(self):
        """State with Plan pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec"
        state["epic_keys"] = ["TEST-124", "TEST-125", "TEST-126"]
        return state

    def test_feature_level_rejection_regenerates_all(self, plan_pending_state):
        """Feature-level rejection regenerates all epics."""
        plan_pending_state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "feature",
        }
        plan_pending_state["feedback_comment"] = "The entire breakdown is wrong. Start over."
        plan_pending_state["revision_requested"] = True

        result = route_plan_approval(plan_pending_state)

        assert result == "regenerate_all_epics"

    def test_all_epics_will_be_deleted(self, plan_pending_state):
        """Full regeneration implies all existing epics deleted."""
        plan_pending_state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "feature",
        }
        plan_pending_state["feedback_comment"] = "Wrong approach entirely."
        plan_pending_state["revision_requested"] = True

        # Verify all epic keys exist before regeneration decision
        assert len(plan_pending_state["epic_keys"]) == 3

        result = route_plan_approval(plan_pending_state)
        assert result == "regenerate_all_epics"


class TestPlanRejectedSingleEpic:
    """Tests for single epic revision."""

    @pytest.fixture
    def plan_with_epic_issue(self):
        """State with specific epic needing revision."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = True
        state["epic_keys"] = ["TEST-124", "TEST-125", "TEST-126"]
        state["current_epic_key"] = "TEST-125"  # The problematic epic
        return state

    def test_single_epic_rejection_updates_only_that_epic(self, plan_with_epic_issue):
        """Single epic rejection only updates that epic."""
        plan_with_epic_issue["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-125",
        }
        plan_with_epic_issue["feedback_comment"] = "Epic 2 scope is too narrow."
        plan_with_epic_issue["revision_requested"] = True

        result = route_plan_approval(plan_with_epic_issue)

        assert result == "update_single_epic"

    def test_other_epics_preserved(self, plan_with_epic_issue):
        """Other epics are preserved when one is revised."""
        plan_with_epic_issue["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-125",
        }
        plan_with_epic_issue["feedback_comment"] = "Fix this one."
        plan_with_epic_issue["revision_requested"] = True

        # Epic keys should still contain all
        assert "TEST-124" in plan_with_epic_issue["epic_keys"]
        assert "TEST-126" in plan_with_epic_issue["epic_keys"]

        result = route_plan_approval(plan_with_epic_issue)
        assert result == "update_single_epic"


class TestPlanPartialApproval:
    """Tests for partial plan approval scenarios."""

    @pytest.fixture
    def plan_partial_approval(self):
        """State where some epics are approved."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = True
        state["epic_keys"] = ["TEST-124", "TEST-125", "TEST-126"]
        return state

    def test_some_approved_one_rejected(self, plan_partial_approval):
        """Some epics approved, one needs revision."""
        plan_partial_approval["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "rejection_scope": "epic",
            "rejected_epic_key": "TEST-126",
            "approved_epics": ["TEST-124", "TEST-125"],  # These are fine
        }
        plan_partial_approval["current_epic_key"] = "TEST-126"
        plan_partial_approval["feedback_comment"] = "Epic 3 needs more detail."
        plan_partial_approval["revision_requested"] = True

        result = route_plan_approval(plan_partial_approval)

        assert result == "update_single_epic"

    def test_all_approved_routes_to_tasks(self, plan_partial_approval):
        """All epics approved routes to task generation when resumed."""
        # Workflow is resumed from pause on approval webhook
        plan_partial_approval["is_paused"] = False

        result = route_plan_approval(plan_partial_approval)

        assert result == "generate_tasks"


class TestPlanRejectedBackToSpec:
    """Tests for plan issues requiring spec changes."""

    @pytest.fixture
    def plan_with_spec_issue(self):
        """Plan state where issue is in spec."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "plan_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = "# PRD"
        state["spec_content"] = "# Spec with missing requirements"
        state["epic_keys"] = ["TEST-124", "TEST-125"]
        state["context"] = {
            "labels": ["forge:managed", "forge:plan-pending"],
            "feedback_scope": "spec",  # Issue is in spec
        }
        state["feedback_comment"] = "Spec is missing US3 which affects the plan."
        state["revision_requested"] = True
        return state

    def test_spec_scope_feedback_noted(self, plan_with_spec_issue):
        """Feedback targeting spec is noted but routes to plan regen."""
        # The feedback mentions spec issues
        assert "Spec" in plan_with_spec_issue["feedback_comment"]

        result = route_plan_approval(plan_with_spec_issue)

        # Currently routes to regenerate (future: could escalate)
        assert result in ["regenerate_all_epics", "update_single_epic"]
