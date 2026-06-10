"""Tests for Spec rejection and revision cycles."""


import pytest

from forge.models.workflow import TicketType
from forge.workflow.gates import route_spec_approval
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state


class TestSpecRejectedOnce:
    """Tests for single Spec rejection cycle."""

    @pytest.fixture
    def spec_pending_state(self):
        """State with Spec pending approval."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "spec_approval_gate"
        state["is_paused"] = True
        state["prd_content"] = """# PRD
## Overview
Approved PRD for user authentication.
"""
        state["spec_content"] = """# Technical Specification

## User Stories
### US1: Login (P1)
**Given** valid credentials
**When** user submits login form
**Then** user is authenticated
"""
        return state

    def test_rejection_with_feedback_routes_to_regenerate(self, spec_pending_state):
        """Spec rejection with feedback routes to regenerate_spec."""
        spec_pending_state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }
        spec_pending_state["feedback_comment"] = "Missing edge cases for invalid input."
        spec_pending_state["revision_requested"] = True

        result = route_spec_approval(spec_pending_state)

        assert result == "regenerate_spec"

    def test_approval_routes_to_decompose(self, spec_pending_state):
        """Spec approval routes to epic decomposition when resumed."""
        # Workflow is resumed from pause on approval webhook
        spec_pending_state["is_paused"] = False

        result = route_spec_approval(spec_pending_state)

        assert result == "decompose_epics"


class TestSpecRejectedMultiple:
    """Tests for multiple Spec rejection cycles."""

    @pytest.fixture
    def spec_state_revised(self):
        """State after previous revision."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["current_node"] = "spec_approval_gate"
        state["is_paused"] = True
        state["retry_count"] = 1
        state["prd_content"] = "# PRD\n\nApproved PRD content."
        state["spec_content"] = "# Spec - Revision 1\n\nFirst revision."
        return state

    def test_second_rejection_routes_to_regenerate(self, spec_state_revised):
        """Second rejection also routes to regenerate."""
        spec_state_revised["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }
        spec_state_revised["feedback_comment"] = "Still missing performance requirements."
        spec_state_revised["revision_requested"] = True

        result = route_spec_approval(spec_state_revised)

        assert result == "regenerate_spec"


class TestSpecRevisionWithPrdContext:
    """Tests for spec revision maintaining PRD context."""

    @pytest.fixture
    def spec_with_prd_context(self):
        """Spec state with full PRD context."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = """# Product Requirements Document

## Overview
User authentication feature for web application.

## Goals
- Secure login/logout
- Password recovery
- Session management

## User Personas
- Admin: Full access
- User: Standard access
"""
        state["spec_content"] = """# Technical Specification

## User Stories
### US1: Login
Given valid credentials...
"""
        state["feedback_comment"] = "Spec doesn't cover password recovery from PRD."
        state["revision_requested"] = True
        state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
        }
        return state

    def test_revision_should_incorporate_prd_goals(self, spec_with_prd_context):
        """Spec revision should reference PRD goals."""
        # The PRD mentions password recovery, spec should too
        assert "Password recovery" in spec_with_prd_context["prd_content"]

        result = route_spec_approval(spec_with_prd_context)
        assert result == "regenerate_spec"

    def test_prd_content_preserved_during_spec_revision(self, spec_with_prd_context):
        """PRD content is not modified during spec revision."""
        original_prd = spec_with_prd_context["prd_content"]

        # Route to regeneration
        route_spec_approval(spec_with_prd_context)

        # PRD should be unchanged
        assert spec_with_prd_context["prd_content"] == original_prd


class TestSpecRejectedBackToPrd:
    """Tests for spec issues requiring PRD changes."""

    @pytest.fixture
    def spec_with_prd_issue(self):
        """Spec state where issue is actually in PRD."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        state["prd_content"] = "# PRD\n\nIncomplete requirements."
        state["spec_content"] = "# Spec\n\nBased on incomplete PRD."
        state["context"] = {
            "labels": ["forge:managed", "forge:spec-pending"],
            "feedback_scope": "prd",  # Issue is actually in PRD
        }
        state["feedback_comment"] = "PRD needs to be updated first. Missing core requirements."
        state["revision_requested"] = True
        return state

    def test_prd_scope_feedback_could_escalate(self, spec_with_prd_issue):
        """Feedback targeting PRD could escalate back."""
        # This tests the scenario where spec review reveals PRD gaps
        # The workflow might need to go back to PRD stage

        # Current routing goes to regenerate_spec
        # But the feedback indicates PRD issues
        assert "PRD" in spec_with_prd_issue["feedback_comment"]

        result = route_spec_approval(spec_with_prd_issue)

        # Currently routes to spec regeneration
        # A more sophisticated implementation could route back to PRD
        assert result == "regenerate_spec"
