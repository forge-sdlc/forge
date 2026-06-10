"""Unit tests for BugState new fields from bug workflow redesign."""

import json

from forge.workflow.bug.state import BugState, create_initial_bug_state


class TestBugStateNewFields:
    """New BugState fields have correct defaults and serialize safely."""

    def test_triage_passed_default_is_false(self):
        """triage_passed defaults to False."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("triage_passed") is False

    def test_triage_missing_fields_default_is_empty_list(self):
        """triage_missing_fields defaults to []."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("triage_missing_fields") == []

    def test_reflection_count_default_is_zero(self):
        """reflection_count defaults to 0."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("reflection_count") == 0

    def test_reflection_critique_default_is_none(self):
        """reflection_critique defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("reflection_critique") is None

    def test_rca_options_default_is_empty_list(self):
        """rca_options defaults to []."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("rca_options") == []

    def test_reproducibility_assessment_default_is_none(self):
        """reproducibility_assessment defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("reproducibility_assessment") is None

    def test_selected_fix_option_default_is_none(self):
        """selected_fix_option defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("selected_fix_option") is None

    def test_selected_fix_approach_default_is_none(self):
        """selected_fix_approach defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("selected_fix_approach") is None

    def test_plan_content_default_is_none(self):
        """plan_content defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("plan_content") is None

    def test_linked_task_keys_default_is_empty_list(self):
        """linked_task_keys defaults to []."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("linked_task_keys") == []

    def test_local_review_verdict_default_is_none(self):
        """local_review_verdict defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("local_review_verdict") is None

    def test_qualitative_feedback_default_is_none(self):
        """qualitative_feedback defaults to None."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("qualitative_feedback") is None

    def test_qualitative_retry_count_default_is_zero(self):
        """qualitative_retry_count defaults to 0."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("qualitative_retry_count") == 0

    def test_qualitative_review_failed_default_is_false(self):
        """qualitative_review_failed defaults to False."""
        state = create_initial_bug_state("BUG-1")
        assert state.get("qualitative_review_failed") is False

    def test_create_initial_bug_state_includes_all_new_fields(self):
        """create_initial_bug_state() explicitly sets every new field."""
        state = create_initial_bug_state("BUG-1")
        new_fields = [
            "triage_passed",
            "triage_missing_fields",
            "reflection_count",
            "reflection_critique",
            "rca_options",
            "reproducibility_assessment",
            "selected_fix_option",
            "selected_fix_approach",
            "plan_content",
            "linked_task_keys",
            "local_review_verdict",
            "qualitative_feedback",
            "qualitative_retry_count",
            "qualitative_review_failed",
        ]
        for field in new_fields:
            assert field in state, f"Field '{field}' missing from create_initial_bug_state()"

    def test_new_fields_serialize_to_json(self):
        """All new fields round-trip through JSON without loss."""
        state = create_initial_bug_state("BUG-1")
        state["triage_passed"] = True
        state["triage_missing_fields"] = ["steps_to_reproduce"]
        state["reflection_count"] = 2
        state["reflection_critique"] = "Missing evidence"
        state["rca_options"] = [{"title": "Fix A", "description": "desc", "tradeoffs": "none"}]
        state["reproducibility_assessment"] = "Unit test feasible"
        state["selected_fix_option"] = 1
        state["selected_fix_approach"] = {"title": "Fix A", "description": "desc", "tradeoffs": "none"}
        state["plan_content"] = "## Plan\nChange src/auth.py"
        state["linked_task_keys"] = ["BUG-2", "BUG-3"]
        state["local_review_verdict"] = "adequate"
        state["qualitative_feedback"] = "Tests are missing"
        state["qualitative_retry_count"] = 1
        state["qualitative_review_failed"] = False

        serialized = json.dumps(dict(state))
        restored = json.loads(serialized)

        assert restored["triage_passed"] is True
        assert restored["triage_missing_fields"] == ["steps_to_reproduce"]
        assert restored["reflection_count"] == 2
        assert restored["reflection_critique"] == "Missing evidence"
        assert len(restored["rca_options"]) == 1
        assert restored["rca_options"][0]["title"] == "Fix A"
        assert restored["selected_fix_option"] == 1
        assert restored["plan_content"] == "## Plan\nChange src/auth.py"
        assert restored["linked_task_keys"] == ["BUG-2", "BUG-3"]
        assert restored["local_review_verdict"] == "adequate"
        assert restored["qualitative_retry_count"] == 1

    def test_legacy_state_dict_missing_new_fields_uses_get_defaults(self):
        """A pre-redesign state dict (missing new keys) safely returns defaults via .get()."""
        # Simulate a state dict from before the redesign (no new fields)
        legacy_state = {
            "thread_id": "BUG-OLD",
            "ticket_key": "BUG-OLD",
            "current_node": "analyze_bug",
            "is_paused": True,
            "rca_content": "Some old RCA",
        }
        # TypedDict is total=False so construction with only old fields is fine
        old_bug_state = BugState(**legacy_state)

        # All new fields should return their expected defaults via .get()
        assert old_bug_state.get("triage_passed", False) is False
        assert old_bug_state.get("triage_missing_fields", []) == []
        assert old_bug_state.get("reflection_count", 0) == 0
        assert old_bug_state.get("reflection_critique", None) is None
        assert old_bug_state.get("rca_options", []) == []
        assert old_bug_state.get("selected_fix_option", None) is None
        assert old_bug_state.get("selected_fix_approach", None) is None
        assert old_bug_state.get("plan_content", None) is None
        assert old_bug_state.get("linked_task_keys", []) == []
        assert old_bug_state.get("local_review_verdict", None) is None
        assert old_bug_state.get("qualitative_retry_count", 0) == 0
        assert old_bug_state.get("qualitative_review_failed", False) is False
        assert old_bug_state.get("reproducibility_assessment", None) is None
        assert old_bug_state.get("qualitative_feedback", None) is None
