"""Tests for PRD PR state fields."""

from forge.models.workflow import TicketType
from forge.workflow.feature.state import FeatureState, create_initial_feature_state


class TestPrdPrStateFields:
    def test_initial_state_has_prd_pr_fields(self):
        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        assert state["prd_pr_url"] is None
        assert state["prd_pr_number"] is None
        assert state["prd_pr_repo"] is None
        assert state["prd_pr_branch"] is None
        assert state["prd_pr_file_path"] is None

    def test_prd_pr_fields_can_be_set(self):
        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            prd_pr_url="https://github.com/org/proposals/pull/5",
            prd_pr_number=5,
            prd_pr_repo="org/proposals",
            prd_pr_branch="forge/prd/test-123",
            prd_pr_file_path="TEST-123/prd.md",
        )
        assert state["prd_pr_url"] == "https://github.com/org/proposals/pull/5"
        assert state["prd_pr_number"] == 5
        assert state["prd_pr_repo"] == "org/proposals"
        assert state["prd_pr_branch"] == "forge/prd/test-123"
        assert state["prd_pr_file_path"] == "TEST-123/prd.md"

    def test_prd_pr_fields_separate_from_implementation_pr(self):
        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        assert state["current_pr_url"] is None
        assert state["current_pr_number"] is None
        assert state["prd_pr_url"] is None
        assert state["prd_pr_number"] is None
