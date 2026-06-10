"""Tests for BaseState and mixins."""

from typing import get_type_hints

import pytest


class TestBaseState:
    """Tests for BaseState TypedDict."""

    def test_base_state_has_required_fields(self):
        """BaseState includes all shared workflow fields."""
        from forge.workflow.base import BaseState

        hints = get_type_hints(BaseState)

        # Identity fields
        assert "thread_id" in hints
        assert "ticket_key" in hints

        # Execution control
        assert "current_node" in hints
        assert "is_paused" in hints
        assert "retry_count" in hints
        assert "last_error" in hints

        # Timestamps
        assert "created_at" in hints
        assert "updated_at" in hints

        # Feedback
        assert "feedback_comment" in hints
        assert "revision_requested" in hints

        # Message history
        assert "messages" in hints
        assert "context" in hints

    def test_base_state_is_total_false(self):
        """BaseState allows partial initialization."""
        from forge.workflow.base import BaseState

        # Should not raise - all fields optional
        state: BaseState = {"thread_id": "test", "ticket_key": "TEST-1"}
        assert state["thread_id"] == "test"


class TestPRIntegrationState:
    """Tests for PR integration mixin."""

    def test_pr_state_has_required_fields(self):
        """PRIntegrationState includes PR-related fields."""
        from forge.workflow.base import PRIntegrationState

        hints = get_type_hints(PRIntegrationState)

        assert "workspace_path" in hints
        assert "pr_urls" in hints
        assert "current_pr_url" in hints
        assert "current_pr_number" in hints
        assert "current_repo" in hints
        assert "repos_to_process" in hints
        assert "repos_completed" in hints
        assert "implemented_tasks" in hints
        assert "current_task_key" in hints


class TestCIIntegrationState:
    """Tests for CI integration mixin."""

    def test_ci_state_has_required_fields(self):
        """CIIntegrationState includes CI-related fields."""
        from forge.workflow.base import CIIntegrationState

        hints = get_type_hints(CIIntegrationState)

        assert "ci_status" in hints
        assert "ci_failed_checks" in hints
        assert "ci_fix_attempts" in hints


class TestReviewIntegrationState:
    """Tests for review integration mixin."""

    def test_review_state_has_required_fields(self):
        """ReviewIntegrationState includes review-related fields."""
        from forge.workflow.base import ReviewIntegrationState

        hints = get_type_hints(ReviewIntegrationState)

        assert "ai_review_status" in hints
        assert "ai_review_results" in hints
        assert "human_review_status" in hints
        assert "pr_merged" in hints


class TestBaseWorkflow:
    """Tests for BaseWorkflow abstract base class."""

    def test_base_workflow_is_abstract(self):
        """BaseWorkflow cannot be instantiated directly."""
        from abc import ABC

        from forge.workflow.base import BaseWorkflow

        assert issubclass(BaseWorkflow, ABC)

        with pytest.raises(TypeError):
            BaseWorkflow()

    def test_base_workflow_has_abstract_methods(self):
        """BaseWorkflow defines required abstract methods."""
        from forge.workflow.base import BaseWorkflow

        abstract_methods = BaseWorkflow.__abstractmethods__
        assert "state_schema" in abstract_methods
        assert "matches" in abstract_methods
        assert "build_graph" in abstract_methods

    def test_create_initial_state_returns_proper_state(self):
        """create_initial_state returns dict with all required BaseState fields."""
        from langgraph.graph import StateGraph

        from forge.workflow.base import BaseWorkflow

        # Create concrete implementation for testing
        class ConcreteWorkflow(BaseWorkflow):
            name = "test"
            description = "test workflow"

            @property
            def state_schema(self):
                from forge.workflow.base import BaseState
                return BaseState

            def matches(self, ticket_type, labels, event):
                return True

            def build_graph(self):
                return StateGraph(self.state_schema)

        workflow = ConcreteWorkflow()
        state = workflow.create_initial_state("TEST-123", extra_field="value")

        # Required fields
        assert state["thread_id"] == "TEST-123"
        assert state["ticket_key"] == "TEST-123"
        assert state["current_node"] == "start"
        assert state["is_paused"] is False
        assert state["retry_count"] == 0
        assert state["last_error"] is None
        assert "created_at" in state
        assert "updated_at" in state
        assert state["messages"] == []
        assert state["context"] == {}

        # Kwargs preserved
        assert state["extra_field"] == "value"
