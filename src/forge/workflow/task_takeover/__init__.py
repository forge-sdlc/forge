"""Task Takeover workflow implementation."""

from typing import Any, cast

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


class TaskTakeoverWorkflow(BaseWorkflow):
    """Workflow for Task Takeover tickets."""

    name = "task_takeover"
    description = "Task Takeover workflow"

    @property
    def state_schema(self) -> type:
        return TaskTakeoverState

    def matches(self, ticket_type: TicketType, labels: list[str], _event: dict[str, Any]) -> bool:
        """Return True for standalone managed Task/Epic tickets."""
        return ticket_type in (TicketType.TASK, TicketType.EPIC) and "forge:managed" in labels

    def build_graph(self) -> StateGraph[Any]:
        """Construct the LangGraph StateGraph for Task Takeover."""
        from forge.workflow.task_takeover.graph import build_task_takeover_graph

        return build_task_takeover_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        """Create initial state for a new Task Takeover workflow run."""
        return cast(dict[str, Any], create_initial_task_takeover_state(ticket_key, **kwargs))


__all__ = ["TaskTakeoverWorkflow", "TaskTakeoverState", "create_initial_task_takeover_state"]
