"""Unit tests for workflow state management."""


from forge.models.workflow import TicketType
from forge.workflow.bug.state import create_initial_bug_state
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state
from forge.workflow.utils import (
    resume_state,
    set_error,
    set_paused,
    update_state_timestamp,
)


class TestCreateInitialState:
    """Tests for create_initial_state function."""

    def test_creates_state_with_required_fields(self):
        """Initial state has all required fields."""
        state = create_initial_state("TEST-123")

        assert state["thread_id"] == "TEST-123"
        assert state["ticket_key"] == "TEST-123"
        assert state["ticket_type"] == TicketType.FEATURE

    def test_initial_state_not_paused(self):
        """Initial state is not paused."""
        state = create_initial_state("TEST-123")

        assert state["is_paused"] is False

    def test_initial_state_at_start_node(self):
        """Initial state starts at 'start' node."""
        state = create_initial_state("TEST-123")

        assert state["current_node"] == "start"

    def test_initial_state_no_errors(self):
        """Initial state has no errors."""
        state = create_initial_state("TEST-123")

        assert state["last_error"] is None
        assert state["retry_count"] == 0

    def test_initial_state_empty_artifacts(self):
        """Initial state has empty artifact fields."""
        state = create_initial_state("TEST-123")

        assert state["prd_content"] == ""
        assert state["spec_content"] == ""
        assert state["epic_keys"] == []
        assert state["task_keys"] == []

    def test_initial_state_has_timestamps(self):
        """Initial state has created_at and updated_at."""
        state = create_initial_state("TEST-123")

        assert "created_at" in state
        assert "updated_at" in state
        assert state["created_at"] == state["updated_at"]

    def test_initial_state_for_bug(self):
        """Initial state works for Bug ticket type."""
        state = create_initial_bug_state("TEST-456")

        assert state["ticket_type"] == TicketType.BUG
        assert state["rca_content"] is None
        assert state["bug_fix_implemented"] is False


class TestUpdateStateTimestamp:
    """Tests for update_state_timestamp function."""

    def test_updates_timestamp(self):
        """Timestamp is updated."""
        state = create_initial_state("TEST-123")
        original_timestamp = state["updated_at"]

        updated = update_state_timestamp(state)

        assert updated["updated_at"] != original_timestamp

    def test_preserves_other_fields(self):
        """Other fields are preserved."""
        state = create_initial_state("TEST-123")
        state["prd_content"] = "Test PRD"

        updated = update_state_timestamp(state)

        assert updated["prd_content"] == "Test PRD"
        assert updated["ticket_key"] == "TEST-123"


class TestSetPaused:
    """Tests for set_paused function."""

    def test_sets_paused_flag(self):
        """is_paused is set to True."""
        state = create_initial_state("TEST-123")

        paused = set_paused(state, "prd_approval_gate")

        assert paused["is_paused"] is True

    def test_sets_current_node(self):
        """current_node is set to the pause node."""
        state = create_initial_state("TEST-123")

        paused = set_paused(state, "spec_approval_gate")

        assert paused["current_node"] == "spec_approval_gate"

    def test_updates_timestamp(self):
        """Timestamp is updated when pausing."""
        state = create_initial_state("TEST-123")
        original = state["updated_at"]

        paused = set_paused(state, "plan_approval_gate")

        assert paused["updated_at"] != original


class TestResumeState:
    """Tests for resume_state function."""

    def test_clears_paused_flag(self):
        """is_paused is set to False."""
        state = create_initial_state("TEST-123")
        state["is_paused"] = True

        resumed = resume_state(state)

        assert resumed["is_paused"] is False

    def test_updates_timestamp(self):
        """Timestamp is updated when resuming."""
        state = create_initial_state("TEST-123")
        state["is_paused"] = True
        original = state["updated_at"]

        resumed = resume_state(state)

        assert resumed["updated_at"] != original


class TestSetError:
    """Tests for set_error function."""

    def test_sets_error_message(self):
        """last_error is set."""
        state = create_initial_state("TEST-123")

        errored = set_error(state, "Connection timeout")

        assert errored["last_error"] == "Connection timeout"

    def test_increments_retry_count(self):
        """retry_count is incremented."""
        state = create_initial_state("TEST-123")
        assert state["retry_count"] == 0

        errored = set_error(state, "First error")
        assert errored["retry_count"] == 1

        errored2 = set_error(errored, "Second error")
        assert errored2["retry_count"] == 2

    def test_updates_timestamp(self):
        """Timestamp is updated on error."""
        state = create_initial_state("TEST-123")
        original = state["updated_at"]

        errored = set_error(state, "Error")

        assert errored["updated_at"] != original


class TestWorkflowStateMutations:
    """Tests for complex state mutations."""

    def test_add_epic_keys(self):
        """Epic keys can be added to state."""
        state = create_initial_state("TEST-123")

        state["epic_keys"] = ["TEST-124", "TEST-125"]

        assert len(state["epic_keys"]) == 2
        assert "TEST-124" in state["epic_keys"]

    def test_add_tasks_by_repo(self):
        """Tasks can be grouped by repository."""
        state = create_initial_state("TEST-123")

        state["tasks_by_repo"] = {
            "org/backend": ["TEST-126", "TEST-127"],
            "org/frontend": ["TEST-128"],
        }

        assert len(state["tasks_by_repo"]["org/backend"]) == 2
        assert len(state["tasks_by_repo"]["org/frontend"]) == 1

    def test_track_implemented_tasks(self):
        """Implemented tasks can be tracked."""
        state = create_initial_state("TEST-123")
        state["task_keys"] = ["TEST-126", "TEST-127", "TEST-128"]
        state["implemented_tasks"] = []

        state["implemented_tasks"].append("TEST-126")
        state["implemented_tasks"].append("TEST-127")

        remaining = [t for t in state["task_keys"] if t not in state["implemented_tasks"]]
        assert remaining == ["TEST-128"]

    def test_parallel_execution_tracking(self):
        """Parallel execution can be tracked."""
        state = create_initial_state("TEST-123")

        state["parallel_execution_enabled"] = True
        state["parallel_branch_id"] = 1
        state["parallel_total_branches"] = 3

        assert state["parallel_execution_enabled"] is True
        assert state["parallel_branch_id"] == 1
        assert state["parallel_total_branches"] == 3
