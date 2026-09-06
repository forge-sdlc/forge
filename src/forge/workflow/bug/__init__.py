"""Locally runnable bug workflow adapter and state contract."""

from typing import Any

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.bug.state import BugState, create_initial_bug_state


class BugWorkflow(BaseWorkflow):
    """Local harness adapter; runtime uses the governed definition."""

    name = "bug"
    description = "Bug workflow: Analyze -> RCA -> Fix -> PR -> Review"

    @property
    def state_schema(self) -> type:
        return BugState

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type == TicketType.BUG

    def build_graph(self) -> StateGraph:
        from forge.workflow.bug.routing import build_bug_graph

        return build_bug_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> BugState:
        return create_initial_bug_state(ticket_key, **kwargs)


__all__ = ["BugWorkflow", "BugState", "create_initial_bug_state"]
