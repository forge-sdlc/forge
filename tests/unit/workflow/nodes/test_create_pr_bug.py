"""Tests for bug-specific PR body content in pr_creation.py."""

from forge.models.workflow import TicketType
from forge.workflow.nodes.pr_creation import _build_pr_body


def _bug_state(**overrides):
    base = {
        "ticket_key": "BUG-42",
        "ticket_type": TicketType.BUG,
        "rca_content": "Password validator rejects special characters.",
        "selected_fix_approach": {
            "title": "Fix regex",
            "description": "Update VALID_PASSWORD_PATTERN to include special chars.",
            "tradeoffs": "Low risk.",
        },
        "plan_content": "## Plan\n\nFix regex in validators.py.",
        "current_repo": "acme/backend",
        "context": {},
        "qualitative_review_failed": False,
        "local_review_verdict": None,
        "qualitative_feedback": None,
    }
    return {**base, **overrides}


def _feature_state(**overrides):
    base = {
        "ticket_key": "FEAT-10",
        "ticket_type": TicketType.FEATURE,
        "context": {"feature_summary": "Add auth flow"},
        "current_repo": "acme/backend",
        "qualitative_review_failed": False,
    }
    return {**base, **overrides}


class TestBugPrBody:
    def test_bug_appends_release_note(self):
        """Bug ticket → PR description includes release note section."""
        body = _build_pr_body(_bug_state(), implemented_tasks=["BUG-50"])
        assert "Release Note" in body

    def test_release_note_includes_component(self):
        """Release note includes Component field."""
        body = _build_pr_body(_bug_state(), implemented_tasks=["BUG-50"])
        assert "Component" in body or "acme/backend" in body

    def test_release_note_includes_fix_description(self):
        """Release note includes fix description from fix approach."""
        body = _build_pr_body(_bug_state(), implemented_tasks=["BUG-50"])
        assert "Fix regex" in body or "VALID_PASSWORD_PATTERN" in body

    def test_feature_no_release_note(self):
        """Feature ticket → no release note section in PR description."""
        body = _build_pr_body(_feature_state(), implemented_tasks=["FEAT-50"])
        assert "Release Note" not in body

    def test_qualitative_review_failed_adds_warning(self):
        """qualitative_review_failed=True → warning block prepended to PR description."""
        state = _bug_state(
            qualitative_review_failed=True,
            local_review_verdict="tests_incomplete",
            qualitative_feedback="Tests do not verify fix.",
        )
        body = _build_pr_body(state, implemented_tasks=["BUG-50"])
        assert "Warning" in body or "warning" in body.lower()
        assert "tests_incomplete" in body

    def test_no_warning_when_review_passed(self):
        """qualitative_review_failed=False → no warning block."""
        body = _build_pr_body(_bug_state(qualitative_review_failed=False), implemented_tasks=["BUG-50"])
        assert "automated qualitative review" not in body.lower()

    def test_warning_and_release_note_both_appear_when_review_failed(self):
        """Both warning block and release note appear when qualitative_review_failed=True on a bug ticket."""
        state = _bug_state(
            qualitative_review_failed=True,
            local_review_verdict="symptom_only",
            qualitative_feedback="Root cause not addressed.",
        )
        body = _build_pr_body(state, implemented_tasks=["BUG-50"])
        assert "Warning" in body or "warning" in body.lower()
        assert "Release Note" in body

    def test_impact_derived_from_rca(self):
        """Impact line in release note is derived from RCA content, not hardcoded."""
        state = _bug_state()
        state["rca_content"] = "Login fails when password contains special characters."
        body = _build_pr_body(state, implemented_tasks=["BUG-50"])
        assert "Login fails when password contains special characters" in body
