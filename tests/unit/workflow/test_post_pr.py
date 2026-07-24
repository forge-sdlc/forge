"""Tests for the shared post-PR lifecycle module."""

import pytest
from langgraph.graph import END


class TestRouteAfterTeardown:
    def test_more_repos_remaining_routes_to_setup(self):
        from forge.workflow.post_pr import _route_after_teardown

        state = {"repos_to_process": ["org/a", "org/b"], "repos_completed": ["org/a"]}
        assert _route_after_teardown(state) == "setup_workspace"

    def test_all_repos_done_routes_to_human_review_gate(self):
        from forge.workflow.post_pr import _route_after_teardown

        state = {"repos_to_process": ["org/a"], "repos_completed": ["org/a"]}
        assert _route_after_teardown(state) == "human_review_gate"

    def test_no_repos_routes_to_human_review_gate(self):
        from forge.workflow.post_pr import _route_after_teardown

        state = {"repos_to_process": [], "repos_completed": []}
        assert _route_after_teardown(state) == "human_review_gate"


class TestRouteCiEvaluation:
    def test_fixing_routes_to_attempt_ci_fix(self):
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "fixing"}) == "attempt_ci_fix"

    def test_failed_routes_to_escalate_blocked(self):
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "failed"}) == "escalate_blocked"

    def test_blocked_routes_to_escalate_blocked(self):
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "blocked"}) == "escalate_blocked"

    def test_passed_returns_to_human_review_gate(self):
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "passed"}) == "human_review_gate"

    def test_pending_returns_to_human_review_gate(self):
        """pending CI no longer returns END — gate re-pauses and waits."""
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "pending"}) == "human_review_gate"

    def test_external_failure_returns_to_human_review_gate(self):
        """External CI failure stays at gate for human to decide."""
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "external_failure"}) == "human_review_gate"

    def test_unknown_status_routes_to_escalate_blocked(self):
        from forge.workflow.post_pr import _route_ci_evaluation

        assert _route_ci_evaluation({"ci_status": "unknown_junk"}) == "escalate_blocked"


class TestRouteAfterPrCreation:
    def test_success_routes_to_teardown(self):
        from forge.workflow.post_pr import route_after_pr_creation

        state = {"pr_urls": ["https://github.com/org/repo/pull/1"], "last_error": None}
        assert route_after_pr_creation(state) == "teardown_workspace"

    def test_failure_with_no_prs_routes_to_escalate(self):
        from forge.workflow.post_pr import route_after_pr_creation

        state = {"pr_urls": [], "last_error": "PR creation failed"}
        assert route_after_pr_creation(state) == "escalate_blocked"

    def test_partial_success_routes_to_teardown(self):
        """If some PRs were created despite an error, proceed to teardown."""
        from forge.workflow.post_pr import route_after_pr_creation

        state = {"pr_urls": ["https://github.com/org/repo/pull/1"], "last_error": "partial"}
        assert route_after_pr_creation(state) == "teardown_workspace"
