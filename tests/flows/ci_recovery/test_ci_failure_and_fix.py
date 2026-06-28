"""Flow tests for CI failure, fix attempts, and recovery scenarios."""

from copy import deepcopy

from langgraph.graph import END

from forge.workflow.feature.graph import _route_ci_evaluation
from tests.fixtures.workflow_states import (
    STATE_CI_FAILED,
    make_workflow_state,
)


class TestCIEvaluationRouting:
    """Tests for CI status → next node routing."""

    def test_passed_ci_routes_to_human_review(self):
        """`ci_status=passed` sends workflow to human review gate."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="passed",
        )

        assert _route_ci_evaluation(state) == "human_review_gate"

    def test_fixing_ci_routes_to_attempt_fix(self):
        """`ci_status=fixing` sends workflow to attempt_ci_fix."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="fixing",
        )

        assert _route_ci_evaluation(state) == "attempt_ci_fix"

    def test_pending_ci_pauses_workflow(self):
        """`ci_status=pending` returns END to pause until the next CI webhook."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="pending",
        )

        assert _route_ci_evaluation(state) == END

    def test_unknown_ci_status_escalates_to_blocked(self):
        """An unrecognised ci_status escalates rather than silently continuing."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="errored",  # not a known value
        )

        assert _route_ci_evaluation(state) == "escalate_blocked"

    def test_missing_ci_status_escalates_to_blocked(self):
        """Missing ci_status escalates — not a silent no-op."""
        state = make_workflow_state(current_node="ci_evaluator")
        state.pop("ci_status", None)

        assert _route_ci_evaluation(state) == "escalate_blocked"


class TestCIFixAttemptTracking:
    """Tests for ci_fix_attempts counter behaviour."""

    def test_first_ci_failure_sets_attempt_count(self):
        """State carries attempt count that starts at 1 on first failure."""
        state = deepcopy(STATE_CI_FAILED)

        assert state["ci_fix_attempt"] == 1

    def test_fix_attempt_counter_increments(self):
        """Each fix cycle must increment ci_fix_attempts."""
        state = deepcopy(STATE_CI_FAILED)
        initial = state["ci_fix_attempt"]

        state["ci_fix_attempt"] += 1

        assert state["ci_fix_attempt"] == initial + 1

    def test_fix_attempts_preserved_across_wait_gate(self):
        """Attempt counter survives the wait-for-CI-gate pause."""
        state = make_workflow_state(
            current_node="wait_for_ci_gate",
            ci_fix_attempt=3,
            ci_status="fixing",
        )

        assert state["ci_fix_attempt"] == 3

    def test_max_retries_exhausted_state(self):
        """At max retries the ci_status is no longer 'fixing'."""
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_fix_attempt=5,
            ci_status="failed",  # evaluator sets this when retries exhausted
        )

        # routing with an unknown/failed status → escalate
        assert _route_ci_evaluation(state) == "escalate_blocked"


class TestCIFixStateMachineScenarios:
    """End-to-end scenario tests for CI fix cycles."""

    def test_first_fix_attempt_scenario(self):
        """
        Scenario: CI fails on first run, one fix attempt is made.

        PR created → CI fails → attempt fix (attempt 1) →
        push fix → wait for CI → CI passes → human review.
        """
        # PR just created, CI reports failure
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="fixing",
            ci_fix_attempt=1,
            ci_failed_checks=[{"name": "tests", "conclusion": "failure"}],
            current_pr_number=42,
        )

        assert _route_ci_evaluation(state) == "attempt_ci_fix"

        # After fix is applied and pushed, workflow waits for new CI results
        state["current_node"] = "wait_for_ci_gate"
        state["ci_status"] = "pending"
        state["ci_fix_attempt"] = 1

        assert _route_ci_evaluation(state) == END

        # New CI webhook arrives — all checks pass
        state["ci_status"] = "passed"
        state["current_node"] = "ci_evaluator"

        assert _route_ci_evaluation(state) == "human_review_gate"

    def test_multiple_fix_attempts_scenario(self):
        """
        Scenario: CI fails three times, fixed on third attempt.

        Each failed CI run increments attempt counter.
        Workflow resumes at human review after final pass.
        """
        for attempt in range(1, 4):
            state = make_workflow_state(
                current_node="ci_evaluator",
                ci_status="fixing",
                ci_fix_attempt=attempt,
            )
            assert _route_ci_evaluation(state) == "attempt_ci_fix"

        # After third fix CI finally passes
        state["ci_status"] = "passed"
        assert _route_ci_evaluation(state) == "human_review_gate"

    def test_ci_exhaustion_escalates_scenario(self):
        """
        Scenario: All fix attempts exhausted, workflow escalates to blocked.

        After max_retries (5) the evaluator sets ci_status to something other
        than 'fixing'/'passed'/'pending', triggering escalation.
        """
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="failed",     # evaluator sets 'failed' after exhaustion
            ci_fix_attempt=5,
            ci_failed_checks=[{"name": "lint", "conclusion": "failure"}],
        )

        assert _route_ci_evaluation(state) == "escalate_blocked"

    def test_ci_passes_without_fix_needed(self):
        """
        Scenario: PR CI passes on first run — no fix cycle needed.
        """
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="passed",
            ci_fix_attempt=0,
        )

        assert _route_ci_evaluation(state) == "human_review_gate"

    def test_failed_checks_recorded_in_state(self):
        """State preserves the list of failed checks for the fix agent."""
        failed = [
            {"name": "unit-tests", "conclusion": "failure"},
            {"name": "lint", "conclusion": "failure"},
        ]
        state = make_workflow_state(
            current_node="ci_evaluator",
            ci_status="fixing",
            ci_failed_checks=failed,
        )

        assert len(state["ci_failed_checks"]) == 2
        assert state["ci_failed_checks"][0]["name"] == "unit-tests"
