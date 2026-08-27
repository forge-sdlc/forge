"""Tests for FeatureState."""

from typing import get_type_hints


class TestFeatureState:
    """Tests for FeatureState TypedDict."""

    def test_feature_state_inherits_base_state(self):
        """FeatureState includes BaseState fields."""
        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)

        # BaseState fields
        assert "thread_id" in hints
        assert "ticket_key" in hints
        assert "current_node" in hints

    def test_feature_state_has_artifact_fields(self):
        """FeatureState includes PRD and spec content."""
        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)

        assert "prd_content" in hints
        assert "spec_content" in hints

    def test_feature_state_has_epic_task_tracking(self):
        """FeatureState includes epic and task tracking."""
        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)

        assert "epic_keys" in hints
        assert "task_keys" in hints
        assert "tasks_by_repo" in hints

    def test_feature_state_has_pr_fields(self):
        """FeatureState includes PR integration fields."""
        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)

        assert "workspace_path" in hints
        assert "pr_urls" in hints

    def test_create_initial_feature_state(self):
        """Can create initial feature state."""
        from forge.workflow.feature.state import create_initial_feature_state

        state = create_initial_feature_state("TEST-123")

        assert state["ticket_key"] == "TEST-123"
        assert state["prd_content"] == ""
        assert state["epic_keys"] == []
        assert state["handoffs"] == {}


class TestQAStateFields:
    """Tests for Q&A mode state fields."""

    def test_feature_state_has_qa_history(self):
        """FeatureState includes qa_history field."""
        from typing import get_type_hints

        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)
        assert "qa_history" in hints

    def test_feature_state_has_generation_context(self):
        """FeatureState includes generation_context field."""
        from typing import get_type_hints

        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)
        assert "generation_context" in hints

    def test_feature_state_has_is_question(self):
        """FeatureState includes is_question field."""
        from typing import get_type_hints

        from forge.workflow.feature.state import FeatureState

        hints = get_type_hints(FeatureState)
        assert "is_question" in hints

    def test_feature_state_qa_defaults(self):
        """FeatureState Q&A fields have correct defaults."""
        from forge.workflow.feature.state import create_initial_feature_state

        state = create_initial_feature_state("TEST-123")

        assert state["qa_history"] == []
        assert state["generation_context"] == {}
        assert state["is_question"] is False
        assert state["handoffs"] == {}

    def test_bug_state_has_qa_history(self):
        """BugState includes qa_history field."""
        from typing import get_type_hints

        from forge.workflow.bug.state import BugState

        hints = get_type_hints(BugState)
        assert "qa_history" in hints

    def test_bug_state_has_generation_context(self):
        """BugState includes generation_context field."""
        from typing import get_type_hints

        from forge.workflow.bug.state import BugState

        hints = get_type_hints(BugState)
        assert "generation_context" in hints

    def test_bug_state_has_is_question(self):
        """BugState includes is_question field."""
        from typing import get_type_hints

        from forge.workflow.bug.state import BugState

        hints = get_type_hints(BugState)
        assert "is_question" in hints

    def test_bug_state_qa_defaults(self):
        """BugState Q&A fields have correct defaults."""
        from forge.workflow.bug.state import create_initial_bug_state

        state = create_initial_bug_state("BUG-456")

        assert state["qa_history"] == []
        assert state["generation_context"] == {}
        assert state["is_question"] is False
