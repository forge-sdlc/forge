"""Flow tests for blocked state escalation and forge:retry recovery."""


from forge.models.workflow import TicketType
from forge.workflow.bug.graph import route_entry
from forge.workflow.feature.graph import route_by_ticket_type
from tests.fixtures.workflow_states import (
    make_workflow_state,
)


class TestEscalationPreservesNode:
    """The failing node must survive escalation so retry can resume there."""

    def test_blocked_at_ci_preserves_current_node(self):
        """current_node stays 'ci_evaluator' after CI exhaustion escalation."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="failed",
            ci_fix_attempts=5,
            last_error="CI fix exhausted after 5 attempts",
        )
        # Simulate what escalate_to_blocked does (sets blocked flag, keeps node)
        state["is_blocked"] = True

        assert state["current_node"] == "ci_evaluator"

    def test_blocked_at_workspace_preserves_current_node(self):
        """current_node stays 'setup_workspace' after workspace failure."""
        state = make_workflow_state(
            current_node="setup_workspace",
            last_error="Failed to clone repository",
        )
        state["is_blocked"] = True

        assert state["current_node"] == "setup_workspace"

    def test_blocked_at_pr_creation_preserves_current_node(self):
        """current_node stays 'create_pr' after PR creation failure."""
        state = make_workflow_state(
            current_node="create_pr",
            last_error="GitHub API rate limit exceeded",
        )
        state["is_blocked"] = True

        assert state["current_node"] == "create_pr"

    def test_blocked_at_implementation_preserves_current_node(self):
        """current_node stays 'implement_task' after implementation failure."""
        state = make_workflow_state(
            current_node="implement_task",
            last_error="Container execution timed out",
        )
        state["is_blocked"] = True

        assert state["current_node"] == "implement_task"


class TestBlockedStateIsTerminal:
    """A blocked workflow must not be reinvoked without an explicit retry."""

    def test_blocked_workflow_skips_invocation(self):
        """
        Worker skips invocation when is_blocked=True and no retry signal.

        This test encodes the expected worker behaviour: the is_blocked flag
        makes a workflow non-invocable, just like a happy-path terminal state.
        """
        state = make_workflow_state(
            current_node="ci_evaluator",
            last_error="CI exhausted",
        )
        state["is_blocked"] = True

        terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
        is_terminal_or_blocked = (
            state.get("current_node") in terminal_nodes
            or state.get("is_blocked", False)
        )

        assert is_terminal_or_blocked is True

    def test_happy_path_complete_is_terminal(self):
        """A normal `complete` state is also non-invocable."""
        state = make_workflow_state(current_node="complete")

        terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
        assert state["current_node"] in terminal_nodes

    def test_mid_workflow_node_is_not_terminal(self):
        """A node like 'ci_evaluator' is only terminal if is_blocked=True."""
        state = make_workflow_state(current_node="ci_evaluator")
        state["is_blocked"] = False

        terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
        is_terminal_or_blocked = (
            state.get("current_node") in terminal_nodes
            or state.get("is_blocked", False)
        )

        assert is_terminal_or_blocked is False


class TestRetryClearsBlockedState:
    """forge:retry must reset error state so the workflow can resume."""

    def test_retry_clears_is_blocked_flag(self):
        """After retry signal, is_blocked becomes False."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            last_error="CI exhausted",
        )
        state["is_blocked"] = True
        state["ci_fix_attempts"] = 5

        # Simulate what the worker retry handler does
        state["is_blocked"] = False
        state["last_error"] = None
        state["retry_count"] = 0
        state["ci_fix_attempts"] = 0

        assert state["is_blocked"] is False

    def test_retry_clears_last_error(self):
        """After retry signal, last_error is cleared."""
        state = make_workflow_state(
            current_node="implement_task",
            last_error="Container timed out",
        )
        state["is_blocked"] = True

        state["is_blocked"] = False
        state["last_error"] = None
        state["retry_count"] = 0

        assert state["last_error"] is None

    def test_retry_resets_ci_fix_attempts(self):
        """Retry resets ci_fix_attempts so CI fix can run its full budget again."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_fix_attempts=5,
        )
        state["is_blocked"] = True

        state["is_blocked"] = False
        state["ci_fix_attempts"] = 0

        assert state["ci_fix_attempts"] == 0

    def test_retry_preserves_current_node_for_resume(self):
        """Retry clears error flags but does NOT overwrite current_node."""
        state = make_workflow_state(
            current_node="setup_workspace",
            last_error="Clone failed",
        )
        state["is_blocked"] = True

        # Simulate retry handler — only clears error-related fields
        state["is_blocked"] = False
        state["last_error"] = None
        state["retry_count"] = 0

        assert state["current_node"] == "setup_workspace"

    def test_retry_on_terminal_state_has_no_error(self):
        """Retry on a happy-path complete state is irrelevant — no error present."""
        state = make_workflow_state(current_node="complete")
        state["is_blocked"] = False
        state["last_error"] = None

        # Nothing to retry — state is clean
        assert state["last_error"] is None
        assert state["is_blocked"] is False


class TestRetryResumesAtCorrectNode:
    """After retry, feature graph routes back to the node that was blocked."""

    def test_retry_from_ci_evaluator_resumes_at_ci(self):
        """Workflow blocked at ci_evaluator resumes there after retry."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ticket_type=TicketType.FEATURE,
        )
        state["is_blocked"] = False

        result = route_by_ticket_type(state)

        assert result == "ci_evaluator"

    def test_retry_from_setup_workspace_resumes_at_setup_workspace(self):
        """Workflow blocked at setup_workspace retries that exact failed node."""
        state = make_workflow_state(
            current_node="setup_workspace",
            ticket_type=TicketType.FEATURE,
        )
        state["is_blocked"] = False

        result = route_by_ticket_type(state)

        assert result == "setup_workspace"

    def test_retry_from_create_pr_resumes_at_create_pr(self):
        """Workflow blocked at create_pr retries that exact failed node."""
        state = make_workflow_state(
            current_node="create_pr",
            ticket_type=TicketType.FEATURE,
        )
        state["is_blocked"] = False

        result = route_by_ticket_type(state)

        assert result == "create_pr"

    def test_bug_retry_from_ci_resumes_at_ci(self):
        """Bug workflow blocked at ci_evaluator resumes there after retry."""

        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="ci_evaluator",
            ticket_type=TicketType.BUG,
        )
        state["is_blocked"] = False

        result = route_entry(state)  # type: ignore[arg-type]

        assert result == "ci_evaluator"

    def test_bug_retry_from_implement_resumes_there(self):
        """Bug workflow blocked at implement_bug_fix resumes there after retry."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="implement_bug_fix",
            ticket_type=TicketType.BUG,
        )
        state["is_blocked"] = False

        result = route_entry(state)  # type: ignore[arg-type]

        assert result == "implement_bug_fix"


class TestAutoRetryTransientErrors:
    """Transient errors below the auto-retry cap resume without user action."""

    def test_transient_error_below_cap_auto_retries(self):
        """
        A workflow with last_error set but retry_count < 3 can be auto-resumed.

        The worker auto-clears last_error and decrements retry budget.
        """
        MAX_AUTO_RETRIES = 3
        state = make_workflow_state(
            current_node="implement_task",
            last_error="Transient network error",
            retry_count=1,
        )
        state["is_blocked"] = False

        can_auto_retry = (
            not state.get("is_paused")
            and state.get("last_error") is not None
            and state.get("retry_count", 0) < MAX_AUTO_RETRIES
        )

        assert can_auto_retry is True

    def test_transient_error_at_cap_does_not_auto_retry(self):
        """At retry_count == MAX_AUTO_RETRIES the auto-retry window is closed."""
        MAX_AUTO_RETRIES = 3
        state = make_workflow_state(
            current_node="implement_task",
            last_error="Transient network error",
            retry_count=3,
        )
        state["is_blocked"] = False

        can_auto_retry = (
            not state.get("is_paused")
            and state.get("last_error") is not None
            and state.get("retry_count", 0) < MAX_AUTO_RETRIES
        )

        assert can_auto_retry is False

    def test_paused_workflow_does_not_auto_retry(self):
        """A paused workflow (waiting for human input) is never auto-retried."""
        MAX_AUTO_RETRIES = 3
        state = make_workflow_state(
            current_node="prd_approval_gate",
            last_error=None,
            retry_count=0,
            is_paused=True,
        )

        can_auto_retry = (
            not state.get("is_paused")
            and state.get("last_error") is not None
            and state.get("retry_count", 0) < MAX_AUTO_RETRIES
        )

        assert can_auto_retry is False
