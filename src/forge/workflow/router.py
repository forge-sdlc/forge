"""Workflow router for matching tickets to workflows."""

from typing import Any

from forge.models.workflow import TicketType
from forge.workflow.base import BaseWorkflow


class WorkflowRouter:
    """Routes incoming tickets to appropriate workflows."""

    def __init__(self) -> None:
        self._workflows: list[type[BaseWorkflow]] = []

    def register(self, workflow_class: type[BaseWorkflow]) -> None:
        """Register a workflow class. First match wins."""
        self._workflows.append(workflow_class)

    def resolve(
        self,
        ticket_type: TicketType,
        labels: list[str],
        event: dict[str, Any],
    ) -> BaseWorkflow | None:
        """Find the first matching workflow for given ticket/event."""
        for workflow_class in self._workflows:
            instance = workflow_class()
            if instance.matches(ticket_type, labels, event):
                return instance
        return None

    def list_workflows(self) -> list[dict[str, str]]:
        """List all registered workflows (for health/debug endpoints)."""
        return [{"name": wf.name, "description": wf.description} for wf in self._workflows]
