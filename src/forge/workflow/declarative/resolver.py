"""Resolve project-scoped declarative workflows from Jira properties."""

from __future__ import annotations

from typing import Any, Protocol

from forge.workflow.declarative.composition import resolve_definition
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import (
    WORKFLOW_LABEL_PREFIX,
    WORKFLOW_NAME_RE,
    WORKFLOW_PROPERTY_PREFIX,
)
from forge.workflow.declarative.node_publication import NodePublisher
from forge.workflow.declarative.workflow import DeclarativeWorkflow


class DefinitionReader(Protocol):
    async def get(self, name: str, revision: int) -> Any | None: ...

    async def active(self, name: str) -> Any | None: ...


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
    is_pinned = (
        pinned_revision is not None or pinned_digest is not None or pinned_definition is not None
    )
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
        published = await definition_reader.active(workflow_name) if definition_reader else None
        if published is not None:
            definition = (
                published if hasattr(published, "digest") else load_workflow_value(published)
            )
        else:
            if jira is None:
                raise ValueError("no active governed workflow definition is available")
            value = await jira.get_project_property(
                project_key.upper(), f"{WORKFLOW_PROPERTY_PREFIX}{workflow_name}"
            )
            if value is None:
                raise ValueError(
                    f"project {project_key.upper()} does not define workflow '{workflow_name}'"
                )
            definition = load_workflow_value(value)
        if definition.spec.includes or any(
            step.node is not None and getattr(step.node, "source", None) == "project"
            for step in definition.spec.steps.values()
        ):
            loaded_dependencies: dict[str, str] = {}
            dependency_cache: dict[str, Any] = {}

            async def dependency(name: str):
                if definition_reader is None:
                    return None
                if name in dependency_cache:
                    return dependency_cache[name]
                value = await definition_reader.active(name)
                resolved = (
                    value
                    if isinstance(value, type(definition))
                    else load_workflow_value(value)
                    if value
                    else None
                )
                if resolved is not None:
                    loaded_dependencies[name] = resolved.digest
                dependency_cache[name] = resolved
                return resolved

            node_reader = (
                definition_reader.node_active
                if definition_reader is not None and hasattr(definition_reader, "node_active")
                else NodePublisher(project_key).active
            )
            loaded_nodes: dict[str, str] = {}

            async def node_dependency(name: str):
                node = await node_reader(name)
                if node is not None:
                    loaded_nodes[name] = node.digest
                return node

            definition = await resolve_definition(
                definition, dependency, node_lookup=node_dependency
            )
            for name, digest in loaded_nodes.items():
                current_node = await node_reader(name)
                if current_node is None or current_node.digest != digest:
                    raise ValueError(f"project node '{name}' changed during resolution")
            for name, digest in loaded_dependencies.items():
                current = await definition_reader.active(name) if definition_reader else None
                if current is None:
                    raise ValueError(f"project dependency '{name}' changed during resolution")
                current = current if hasattr(current, "digest") else load_workflow_value(current)
                if current.digest != digest:
                    raise ValueError(f"project dependency '{name}' changed during resolution")
    if definition.metadata.name != workflow_name:
        raise ValueError(
            f"workflow property name '{workflow_name}' does not match metadata name "
            f"'{definition.metadata.name}'"
        )
    return DeclarativeWorkflow(definition, project_key)
