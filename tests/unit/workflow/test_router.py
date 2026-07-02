"""Tests for WorkflowRouter."""

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseState, BaseWorkflow


class MockWorkflow(BaseWorkflow):
    """Test workflow that matches Features."""

    name = "mock"
    description = "Mock workflow for testing"

    @property
    def state_schema(self) -> type:
        return BaseState

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict) -> bool:
        return ticket_type == TicketType.FEATURE

    def build_graph(self) -> StateGraph:
        graph = StateGraph(BaseState)
        graph.add_node("start", lambda s: s)
        graph.set_entry_point("start")
        return graph


class MockBugWorkflow(BaseWorkflow):
    """Test workflow that matches Bugs."""

    name = "mock_bug"
    description = "Mock bug workflow for testing"

    @property
    def state_schema(self) -> type:
        return BaseState

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict) -> bool:
        return ticket_type == TicketType.BUG

    def build_graph(self) -> StateGraph:
        graph = StateGraph(BaseState)
        graph.add_node("start", lambda s: s)
        graph.set_entry_point("start")
        return graph


class TestWorkflowRouter:
    """Tests for WorkflowRouter."""

    def test_register_workflow(self):
        """Can register a workflow class."""
        from forge.workflow.router import WorkflowRouter

        router = WorkflowRouter()
        router.register(MockWorkflow)

        assert len(router.list_workflows()) == 1

    def test_resolve_returns_matching_workflow(self):
        """Resolve returns workflow that matches ticket."""
        from forge.workflow.router import WorkflowRouter

        router = WorkflowRouter()
        router.register(MockWorkflow)
        router.register(MockBugWorkflow)

        workflow = router.resolve(
            ticket_type=TicketType.FEATURE,
            labels=[],
            event={},
        )

        assert workflow is not None
        assert workflow.name == "mock"

    def test_resolve_returns_none_when_no_match(self):
        """Resolve returns None when no workflow matches."""
        from forge.workflow.router import WorkflowRouter

        router = WorkflowRouter()
        router.register(MockWorkflow)

        workflow = router.resolve(
            ticket_type=TicketType.BUG,
            labels=[],
            event={},
        )

        assert workflow is None

    def test_resolve_first_match_wins(self):
        """First registered workflow that matches is returned."""
        from forge.workflow.router import WorkflowRouter

        class AnotherFeatureWorkflow(MockWorkflow):
            name = "another_feature"

        router = WorkflowRouter()
        router.register(MockWorkflow)
        router.register(AnotherFeatureWorkflow)

        workflow = router.resolve(
            ticket_type=TicketType.FEATURE,
            labels=[],
            event={},
        )

        assert workflow.name == "mock"

    def test_list_workflows(self):
        """List returns all registered workflows."""
        from forge.workflow.router import WorkflowRouter

        router = WorkflowRouter()
        router.register(MockWorkflow)
        router.register(MockBugWorkflow)

        workflows = router.list_workflows()

        assert len(workflows) == 2
        assert workflows[0]["name"] == "mock"
        assert workflows[1]["name"] == "mock_bug"

    def test_task_takeover_resolves_by_ticket_type_and_managed_label(self):
        """TaskTakeoverWorkflow resolves managed Task/Epic tickets only."""
        from forge.workflow.router import WorkflowRouter
        from forge.workflow.task_takeover import TaskTakeoverWorkflow

        router = WorkflowRouter()
        router.register(TaskTakeoverWorkflow)

        workflow = router.resolve(
            ticket_type=TicketType.TASK,
            labels=["forge:managed"],
            event={},
        )
        assert workflow is not None
        assert workflow.name == "task_takeover"

        workflow = router.resolve(
            ticket_type=TicketType.EPIC,
            labels=["forge:managed"],
            event={},
        )
        assert workflow is not None
        assert workflow.name == "task_takeover"

        assert router.resolve(TicketType.BUG, ["forge:managed", "forge:managed:task"], {}) is None
