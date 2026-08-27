"""Resolve project-scoped declarative workflows from Jira properties."""

from __future__ import annotations

from typing import Any, Protocol

from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import (
    WORKFLOW_LABEL_PREFIX,
    WORKFLOW_NAME_RE,
    WORKFLOW_PROPERTY_PREFIX,
)
from forge.workflow.declarative.workflow import DeclarativeWorkflow


class DefinitionReader(Protocol):
    async def get(self, name: str, revision: int) -> Any | None: ...


class ProjectPropertyReader(Protocol):
    async def get_project_property(self, project_key: str, property_key: str) -> Any | None: ...


def selected_workflow_name(labels: list[str]) -> str | None:
    selected = sorted(
        {
            label[len(WORKFLOW_LABEL_PREFIX) :]
            for label in labels
            if label.startswith(WORKFLOW_LABEL_PREFIX)
        }
    )
    if len(selected) > 1:
        raise ValueError(f"multiple custom workflows selected: {', '.join(selected)}")
    if not selected:
        return None
    if not WORKFLOW_NAME_RE.fullmatch(selected[0]):
        raise ValueError(f"invalid custom workflow label: {WORKFLOW_LABEL_PREFIX}{selected[0]}")
    return selected[0]


async def load_project_workflow(
    jira: ProjectPropertyReader | None,
    project_key: str,
    workflow_name: str,
    *,
    pinned_revision: int | None = None,
    pinned_digest: str | None = None,
    pinned_definition: dict[str, Any] | None = None,
    definition_reader: DefinitionReader | None = None,
) -> DeclarativeWorkflow:
    """Resolve an active workflow, or an exact immutable pinned artifact.

    A checkpoint's canonical definition is preferred because it is the durable
    source of truth for an in-flight instance.  If only identity metadata was
    persisted, ``definition_reader`` must provide the exact published revision;
    this function deliberately never falls back to Jira's active property for a
    pinned checkpoint.
    """
    is_pinned = pinned_revision is not None or pinned_digest is not None or pinned_definition is not None
    if is_pinned:
        if pinned_revision is None or not pinned_digest:
            raise ValueError("pinned workflow identity requires both revision and digest")
        if pinned_definition is not None:
            definition = load_workflow_value(pinned_definition)
        else:
            if definition_reader is None:
                raise ValueError(
                    f"published workflow '{workflow_name}' revision {pinned_revision} is unavailable"
                )
            value = await definition_reader.get(workflow_name, int(pinned_revision))
            if value is None:
                raise ValueError(
                    f"published workflow '{workflow_name}' revision {pinned_revision} is unavailable"
                )
            definition = value if hasattr(value, "digest") else load_workflow_value(value)
        if definition.metadata.name != workflow_name:
            raise ValueError("pinned workflow definition name does not match checkpoint")
        if definition.metadata.revision != int(pinned_revision):
            raise ValueError("pinned workflow definition revision does not match checkpoint")
        if definition.digest != pinned_digest:
            raise ValueError("pinned workflow definition digest does not match checkpoint")
    else:
        if jira is None:
            raise ValueError("Jira property reader is required for a new workflow instance")
        value = await jira.get_project_property(
            project_key.upper(), f"{WORKFLOW_PROPERTY_PREFIX}{workflow_name}"
        )
        if value is None:
            raise ValueError(
                f"project {project_key.upper()} does not define workflow '{workflow_name}'"
            )
        definition = load_workflow_value(value)
    if definition.metadata.name != workflow_name:
        raise ValueError(
            f"workflow property name '{workflow_name}' does not match metadata name "
            f"'{definition.metadata.name}'"
        )
    return DeclarativeWorkflow(definition, project_key)
