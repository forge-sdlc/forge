"""Tests for FeatureWorkflow."""


from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.feature.graph import (
    _route_after_epic_task_regeneration,
    route_by_ticket_type,
)


class TestFeatureWorkflow:
    """Tests for FeatureWorkflow class."""

    def test_workflow_has_name(self):
        """FeatureWorkflow has name attribute."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        assert workflow.name == "feature"

    def test_workflow_has_description(self):
        """FeatureWorkflow has description."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        assert "PRD" in workflow.description

    def test_matches_feature_type(self):
        """Matches Feature ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.FEATURE, [], {}) is True

    def test_matches_story_type(self):
        """Matches Story ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.STORY, [], {}) is True

    def test_does_not_match_bug(self):
        """Does not match Bug ticket type."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()

        assert workflow.matches(TicketType.BUG, [], {}) is False

    def test_state_schema_returns_feature_state(self):
        """state_schema returns FeatureState."""
        from forge.workflow.feature import FeatureWorkflow
        from forge.workflow.feature.state import FeatureState

        workflow = FeatureWorkflow()

        assert workflow.state_schema is FeatureState

    def test_build_graph_returns_state_graph(self):
        """build_graph returns a StateGraph."""
        from langgraph.graph import StateGraph

        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        graph = workflow.build_graph()

        assert isinstance(graph, StateGraph)

    def test_create_initial_state(self):
        """create_initial_state returns FeatureState with defaults."""
        from forge.workflow.feature import FeatureWorkflow

        workflow = FeatureWorkflow()
        state = workflow.create_initial_state("TEST-123")

        assert state["ticket_key"] == "TEST-123"
        assert state["ticket_type"] == TicketType.FEATURE
        assert state["prd_content"] == ""

    def test_resume_regenerate_all_epics_stays_on_regeneration_node(self):
        """Retrying full plan regeneration should not restart raw epic decomposition."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_all_epics",
        }

        assert route_by_ticket_type(state) == "regenerate_all_epics"

    def test_resume_update_single_epic_stays_on_update_node(self):
        """Retrying an Epic-level plan update should not create new epics."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "update_single_epic",
        }

        assert route_by_ticket_type(state) == "update_single_epic"

    def test_resume_regenerate_prd_stays_on_regeneration_node(self):
        """Retrying PRD revision should not restart initial PRD generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_prd",
        }

        assert route_by_ticket_type(state) == "regenerate_prd"

    def test_resume_regenerate_spec_stays_on_regeneration_node(self):
        """Retrying spec revision should not restart initial spec generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_spec",
        }

        assert route_by_ticket_type(state) == "regenerate_spec"

    def test_resume_regenerate_all_tasks_stays_on_regeneration_node(self):
        """Retrying full task regeneration should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_all_tasks",
        }

        assert route_by_ticket_type(state) == "regenerate_all_tasks"

    def test_resume_update_single_task_stays_on_update_node(self):
        """Retrying a Task-level update should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "update_single_task",
        }

        assert route_by_ticket_type(state) == "update_single_task"

    def test_resume_implement_task_stays_on_implementation_node(self):
        """Retrying feature implementation should not reroute through task_router."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implement_task",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_legacy_implementation_alias_stays_on_implementation_node(self):
        """Legacy feature implementation checkpoints should retry implementation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implementation",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_polluted_bug_implementation_node_stays_on_implementation_node(self):
        """Feature checkpoints polluted with bug node names should not restart PRD generation."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implement_bug_fix",
        }

        assert route_by_ticket_type(state) == "implement_task"

    def test_resume_regenerate_epic_tasks_stays_on_regeneration_node(self):
        """Retrying epic-level task regeneration should not restart the workflow."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_epic_tasks",
        }

        assert route_by_ticket_type(state) == "regenerate_epic_tasks"

    def test_failed_regenerate_epic_tasks_preserves_retry_node(self):
        """Failed epic-level task regeneration should not re-enter the approval gate."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "regenerate_epic_tasks",
            "last_error": "No replacement Tasks generated",
        }

        assert _route_after_epic_task_regeneration(state) == END

    def test_successful_regenerate_epic_tasks_returns_to_task_approval_gate(self):
        """Successful epic-level task regeneration returns to task approval."""
        state = {
            "ticket_key": "TEST-123",
            "ticket_type": TicketType.FEATURE,
            "current_node": "task_approval_gate",
            "last_error": None,
        }

        assert _route_after_epic_task_regeneration(state) == "task_approval_gate"
