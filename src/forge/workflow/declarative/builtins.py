"""Versioned definitions for Forge-supported golden paths.

The built-in workflows are checked-in process artifacts. Keeping the source
documents separate from this adapter makes the artifacts inspectable and
ensures the runtime cannot silently invent or mutate workflow topology.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from forge.models.workflow import TicketType
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import WorkflowDefinition
from forge.workflow.declarative.workflow import DeclarativeWorkflow

POLICY = "forge-contracts-v1"
JIRA_EFFECTS = ("jira.*",)
SC_EFFECTS = ("source_control.*",)


def _load_builtin_definition(name: str) -> WorkflowDefinition:
    """Load and validate a checked-in built-in process artifact by name."""
    resource = resources.files("forge.workflow.declarative.definitions").joinpath(f"{name}.json")
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing built-in workflow artifact: {name}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid built-in workflow artifact: {name}") from exc
    if value.get("kind") == "Workflow" and value.get("spec", {}).get("includes"):
        # Built-in artifacts are resolved synchronously because their graphs are
        # also constructed inside an already-running event loop.
        includes = value["spec"].pop("includes")
        dependencies = []
        for include in includes:
            if include["source"] != "builtin":
                raise RuntimeError("built-in workflows may include only built-in subworkflows")
            fragment = _load_builtin_definition(include["name"])
            if fragment.kind != "Subworkflow":
                raise RuntimeError("built-in include must be a subworkflow")
            dependencies.append(
                f"builtin:{fragment.metadata.name}:{fragment.metadata.revision}:{fragment.digest}"
            )
            for node, step in fragment.spec.steps.items():
                if node in value["spec"]["steps"]:
                    raise RuntimeError(f"duplicate built-in node '{node}'")
                imported = step.model_dump(by_alias=True, exclude_none=True)
                if imported.get("next", "").startswith("@exit/"):
                    imported["next"] = include["exits"][imported["next"][6:]]
                if "branches" in imported:
                    imported["branches"] = {
                        outcome: include["exits"].get(target[6:], target)
                        if target.startswith("@exit/")
                        else target
                        for outcome, target in imported["branches"].items()
                    }
                value["spec"]["steps"][node] = imported
        value["spec"]["resolvedDependencies"] = dependencies
    definition = load_workflow_value(value)
    if definition.metadata.name != name:
        raise RuntimeError(
            f"built-in workflow artifact {name!r} declares name {definition.metadata.name!r}"
        )
    return definition


def load_builtin_source(name: str) -> WorkflowDefinition:
    """Load a checked-in workflow or reusable subworkflow by exact name."""
    if name not in {"feature", "bug", "task_takeover", "github_pr_review"}:
        raise ValueError(f"unknown built-in definition '{name}'")
    return _load_builtin_definition(name)


def builtin_feature_definition() -> WorkflowDefinition:
    """Return the immutable feature golden-path definition."""
    return _load_builtin_definition("feature")


def builtin_definitions() -> tuple[WorkflowDefinition, ...]:
    return (
        builtin_feature_definition(),
        builtin_bug_definition(),
        builtin_task_takeover_definition(),
    )


class FeatureGoldenWorkflow(DeclarativeWorkflow):
    """Default Feature/Story runtime compiled from the published process model."""

    name = "feature"
    description = "Full SDLC workflow compiled from the versioned feature definition"

    def __init__(self) -> None:
        super().__init__(builtin_feature_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in {TicketType.FEATURE, TicketType.STORY}


def builtin_bug_definition() -> WorkflowDefinition:
    """Return the immutable bug-fix golden-path definition."""
    return _load_builtin_definition("bug")


class BugGoldenWorkflow(DeclarativeWorkflow):
    name = "bug"
    description = "Bug-fix workflow compiled from the versioned process definition"

    def __init__(self) -> None:
        super().__init__(builtin_bug_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, _labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type is TicketType.BUG


def builtin_task_takeover_definition() -> WorkflowDefinition:
    """Return the immutable task-takeover golden-path definition."""
    return _load_builtin_definition("task_takeover")


class TaskTakeoverGoldenWorkflow(DeclarativeWorkflow):
    name = "task_takeover"
    description = "Task-takeover workflow compiled from the versioned process definition"

    def __init__(self) -> None:
        super().__init__(builtin_task_takeover_definition(), "BUILTIN")

    @property
    def cache_key(self) -> str:
        return f"builtin:{self.name}:{self.definition.metadata.revision}:{self.definition.digest}"

    def matches(self, ticket_type: TicketType, labels: list[str], _event: dict[str, Any]) -> bool:
        return ticket_type in {TicketType.TASK, TicketType.EPIC} and "forge:managed" in labels
