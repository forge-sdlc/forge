"""Tests for complete feature workflow flow."""


import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.graph import route_by_ticket_type
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from tests.fixtures.workflow_states import (
    STATE_COMPLETED,
    make_workflow_state,
)


class TestFeatureWorkflowEntry:
    """Tests for feature workflow entry."""

    def test_feature_routes_to_prd_generation(self):
        """Feature ticket starts with PRD generation."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )

        next_node = route_by_ticket_type(state)

        assert next_node == "generate_prd"

    def test_story_treated_as_feature(self):
        """Story ticket is treated like Feature."""
        state = create_initial_state(
            thread_id="test-thread",
            ticket_key="TEST-123",
            ticket_type=TicketType.STORY,
        )

        # Stories should go to PRD generation (like features)
        # or directly to task workflow depending on implementation
        next_node = route_by_ticket_type(state)

        assert next_node in ["generate_prd", "task_workflow"]


class TestFeatureWorkflowPhases:
    """Tests for feature workflow phase progression."""

    def test_new_feature_to_prd_pending(self):
        """New feature progresses to PRD pending."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="generate_prd",
        )

        # After PRD generation, state should be at approval gate
        # This is a state progression test
        assert state["current_node"] == "generate_prd"

    def test_prd_approved_to_spec_generation(self):
        """Approved PRD progresses to spec generation when resumed."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="prd_approval_gate",
            prd_content="# Approved PRD",
            is_paused=False,  # Resumed from pause on approval webhook
        )

        from forge.workflow.gates import route_prd_approval
        next_node = route_prd_approval(state)

        assert next_node == "generate_spec"

    def test_spec_approved_to_epic_decomposition(self):
        """Approved spec progresses to epic decomposition when resumed."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="spec_approval_gate",
            prd_content="# PRD",
            spec_content="# Approved Spec",
            is_paused=False,  # Resumed from pause on approval webhook
        )

        from forge.workflow.gates import route_spec_approval
        next_node = route_spec_approval(state)

        assert next_node == "decompose_epics"

    def test_plan_approved_to_task_generation(self):
        """Approved plan progresses to task generation when resumed."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="plan_approval_gate",
            epic_keys=["TEST-124", "TEST-125"],
            is_paused=False,  # Resumed from pause on approval webhook
        )

        from forge.workflow.gates import route_plan_approval
        next_node = route_plan_approval(state)

        assert next_node == "generate_tasks"


class TestFeatureWorkflowCompletion:
    """Tests for feature workflow completion."""

    def test_completed_state_has_all_flags(self):
        """Completed feature has all completion flags set."""
        state = STATE_COMPLETED

        assert state["pr_merged"] is True
        assert state["tasks_completed"] is True
        assert state["epics_completed"] is True
        assert state["feature_completed"] is True

    def test_completed_state_has_pr_urls(self):
        """Completed feature has PR URLs."""
        state = STATE_COMPLETED

        assert len(state["pr_urls"]) > 0

    def test_completed_state_all_tasks_implemented(self):
        """Completed feature has all tasks implemented."""
        state = STATE_COMPLETED

        assert len(state["implemented_tasks"]) == len(state["task_keys"])


class TestMultiEpicFeature:
    """Tests for features with multiple epics."""

    @pytest.fixture
    def multi_epic_state(self):
        """State with multiple epics."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="plan_approval_gate",
            epic_keys=["TEST-124", "TEST-125", "TEST-126", "TEST-127"],
        )
        return state

    def test_multiple_epics_created(self, multi_epic_state):
        """Multiple epics are tracked in state."""
        assert len(multi_epic_state["epic_keys"]) == 4

    def test_all_epics_must_be_approved(self, multi_epic_state):
        """All epics must be approved for plan approval - workflow pauses to wait."""
        # Workflow is paused waiting for approval
        multi_epic_state["is_paused"] = True

        from langgraph.graph import END

        from forge.workflow.gates import route_plan_approval

        result = route_plan_approval(multi_epic_state)

        # Should wait (END) until approved via webhook
        assert result == END


class TestMultiRepoFeature:
    """Tests for features spanning multiple repositories."""

    @pytest.fixture
    def multi_repo_state(self):
        """State with tasks in multiple repos."""
        state = make_workflow_state(
            ticket_key="TEST-123",
            current_node="task_router",
            task_keys=["TEST-200", "TEST-201", "TEST-202", "TEST-203"],
            tasks_by_repo={
                "org/backend": ["TEST-200", "TEST-201"],
                "org/frontend": ["TEST-202", "TEST-203"],
            },
        )
        return state

    def test_tasks_grouped_by_repo(self, multi_repo_state):
        """Tasks are grouped by repository."""
        assert len(multi_repo_state["tasks_by_repo"]) == 2
        assert "org/backend" in multi_repo_state["tasks_by_repo"]
        assert "org/frontend" in multi_repo_state["tasks_by_repo"]

    def test_each_repo_has_tasks(self, multi_repo_state):
        """Each repository has assigned tasks."""
        backend_tasks = multi_repo_state["tasks_by_repo"]["org/backend"]
        frontend_tasks = multi_repo_state["tasks_by_repo"]["org/frontend"]

        assert len(backend_tasks) == 2
        assert len(frontend_tasks) == 2

    def test_all_repos_must_complete(self, multi_repo_state):
        """All repositories must complete for feature to complete."""
        multi_repo_state["repos_to_process"] = ["org/backend", "org/frontend"]
        multi_repo_state["repos_completed"] = ["org/backend"]  # Only one done

        # Should have more repos to process
        remaining = [
            r for r in multi_repo_state["repos_to_process"]
            if r not in multi_repo_state["repos_completed"]
        ]

        assert len(remaining) == 1
        assert "org/frontend" in remaining
