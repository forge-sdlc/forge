"""BaseWorkflow adapter for a validated declarative definition."""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow
from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler, WorkflowValidationError
from forge.workflow.declarative.models import WorkflowDefinition


class DeclarativeWorkflow(BaseWorkflow):
    def __init__(self, definition: WorkflowDefinition, project_key: str) -> None:
        self.definition = definition
        self.project_key = project_key.upper()
        self.name = definition.metadata.name
        self.description = definition.metadata.description
        self._profile = get_state_profile(definition.spec.state)
        DeclarativeWorkflowCompiler(definition).validate()

    @property
    def cache_key(self) -> str:
        return (
            f"custom:{self.project_key}:{self.name}:"
            f"{self.definition.metadata.revision}:{self.definition.digest}"
        )

    @property
    def state_schema(self) -> type:
        return self._profile.schema

    def matches(
        self,
        _ticket_type: TicketType,
        _labels: list[str],
        _event: dict[str, Any],
    ) -> bool:
        return False

    def supports_ticket_type(self, ticket_type: TicketType) -> bool:
        supported = {
            "feature": {TicketType.FEATURE, TicketType.STORY},
            "bug": {TicketType.BUG},
            "task_takeover": {TicketType.TASK, TicketType.EPIC},
        }
        return (
            ticket_type == TicketType.UNKNOWN
            or ticket_type in supported[self.definition.spec.state]
        )

    def build_graph(self) -> StateGraph[Any]:
        return DeclarativeWorkflowCompiler(self.definition).build_graph()

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        state = dict(self._profile.initializer(ticket_key, **kwargs))
        state.update(self.workflow_metadata())
        return state

    def workflow_metadata(self) -> dict[str, Any]:
        return {
            "workflow_name": self.name,
            "workflow_revision": self.definition.metadata.revision,
            "workflow_digest": self.definition.digest,
            "workflow_definition": self.definition.canonical_dict(),
            "workflow_state_profile": self.definition.spec.state,
            "workflow_project_key": self.project_key,
            "workflow_transition_count": 0,
        }

    def migrate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Adopt this definition while refusing ambiguous or unsafe migration."""
        if not state.get("workflow_name"):
            return state
        if state.get("workflow_name") != self.name:
            raise WorkflowValidationError("an active checkpoint cannot switch workflow identity")
        if state.get("workflow_state_profile") != self.definition.spec.state:
            raise WorkflowValidationError("an active workflow cannot change state profile")

        old_revision = int(state.get("workflow_revision", 0))
        old_digest = state.get("workflow_digest")
        new_revision = self.definition.metadata.revision
        if old_revision == new_revision and old_digest != self.definition.digest:
            raise WorkflowValidationError("workflow content changed without incrementing revision")
        if new_revision < old_revision:
            raise WorkflowValidationError("workflow revision rollback is not allowed")

        migrated = {**state, **self.workflow_metadata()}
        migrated["workflow_transition_count"] = state.get("workflow_transition_count", 0)
        current_node = str(state.get("current_node", ""))
        control_nodes = {"", "start", "entry", "__end__", "complete"}
        if current_node not in control_nodes and current_node not in self.definition.spec.steps:
            mapping = self.definition.spec.resume.from_revisions.get(old_revision, {})
            if current_node not in mapping:
                raise WorkflowValidationError(
                    f"revision {new_revision} removed saved node '{current_node}' without a "
                    f"resume mapping from revision {old_revision}"
                )
            migrated["current_node"] = mapping[current_node]
        return migrated
