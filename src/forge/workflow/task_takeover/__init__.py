"""Locally runnable task-takeover workflow adapter and state contract."""

from typing import Any, cast

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


class TaskTakeoverWorkflow(BaseWorkflow):
    """Local harness adapter; runtime uses the governed definition."""

    name = "task_takeover"
    description = "Task Takeover workflow"

    @property
    def state_schema(self) -> type:
        return TaskTakeoverState

    def matches(self, ticket_type: TicketType, labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in (TicketType.TASK, TicketType.EPIC) and "forge:managed" in labels

    def build_graph(self) -> StateGraph[Any]:
        from forge.workflow.task_takeover.graph import build_task_takeover_graph

        return build_task_takeover_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], create_initial_task_takeover_state(ticket_key, **kwargs))


__all__ = ["TaskTakeoverWorkflow", "TaskTakeoverState", "create_initial_task_takeover_state"]
