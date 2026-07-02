"""Tests for TaskTakeoverWorkflow."""

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.task_takeover import TaskTakeoverWorkflow
from forge.workflow.task_takeover.state import TaskTakeoverState


class TestTaskTakeoverWorkflow:
    """Tests for TaskTakeoverWorkflow class."""

    def test_workflow_has_name(self):
        """TaskTakeoverWorkflow has name attribute."""
        workflow = TaskTakeoverWorkflow()
        assert workflow.name == "task_takeover"

    def test_workflow_has_description(self):
        """TaskTakeoverWorkflow has description."""
        workflow = TaskTakeoverWorkflow()
        assert workflow.description == "Task Takeover workflow"

    def test_state_schema_returns_task_takeover_state(self):
        """state_schema returns TaskTakeoverState."""
        workflow = TaskTakeoverWorkflow()
        assert workflow.state_schema is TaskTakeoverState

    def test_build_graph_returns_state_graph(self):
        """build_graph returns a StateGraph."""
        workflow = TaskTakeoverWorkflow()
        graph = workflow.build_graph()
        assert isinstance(graph, StateGraph)

    def test_create_initial_state(self):
        """create_initial_state returns TaskTakeoverState with defaults."""
        workflow = TaskTakeoverWorkflow()
        state = workflow.create_initial_state("TASK-123")

        assert state["ticket_key"] == "TASK-123"
        assert state["ticket_type"] == TicketType.TASK
        assert state["current_node"] == "start"

    def test_matches_standalone_managed_task_and_epic(self):
        """matches returns True for managed standalone Task and Epic tickets."""
        workflow = TaskTakeoverWorkflow()

        assert workflow.matches(TicketType.TASK, ["forge:managed"], {}) is True
        assert workflow.matches(TicketType.EPIC, ["forge:managed"], {}) is True

    def test_matches_returns_false_for_feature_and_bug(self):
        """matches returns False for non-takeover ticket types."""
        workflow = TaskTakeoverWorkflow()
        assert workflow.matches(TicketType.FEATURE, ["forge:managed"], {}) is False
        assert workflow.matches(TicketType.BUG, ["forge:managed"], {}) is False

    def test_matches_requires_forge_managed(self):
        """matches returns False without the forge:managed opt-in label."""
        workflow = TaskTakeoverWorkflow()
        assert workflow.matches(TicketType.TASK, ["forge:managed-something"], {}) is False
        assert workflow.matches(TicketType.TASK, ["other-label"], {}) is False
