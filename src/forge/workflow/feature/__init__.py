"""Locally runnable feature workflow adapter and state contract."""

from typing import Any

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.feature.state import FeatureState, create_initial_feature_state


class FeatureWorkflow(BaseWorkflow):
    """Local harness adapter; runtime uses the governed definition."""

    name = "feature"
    description = "Full SDLC workflow: PRD -> Spec -> Epic -> Task -> Implementation"

    @property
    def state_schema(self) -> type:
        return FeatureState

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in (TicketType.FEATURE, TicketType.STORY)

    def build_graph(self) -> StateGraph:
        from forge.workflow.feature.graph import build_feature_graph

        return build_feature_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> FeatureState:
        return create_initial_feature_state(ticket_key, **kwargs)


__all__ = ["FeatureWorkflow", "FeatureState", "create_initial_feature_state"]
