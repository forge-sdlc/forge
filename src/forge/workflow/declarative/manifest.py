"""Runtime-independent inspection and revision impact for workflow definitions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from forge.domain import DomainModel
from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.models import WorkflowDefinition


class ProcessNodeKind(StrEnum):
    STATION = "station"
    GATE = "gate"
    OPERATION = "operation"


class ProcessTransition(DomainModel):
    source: str
    target: str
    outcome: str | None = None


class ProcessNode(DomainModel):
    name: str
    kind: ProcessNodeKind
    station_contract: str | None = None
    station_contract_version: str | None = None
    required_policies: tuple[str, ...] = ()
    allowed_effects: tuple[str, ...] = ()
    join: str | None = None
    max_concurrency: int | None = None
    retry_bound: int | None = None


class ProcessManifest(DomainModel):
    workflow_name: str
    revision: int
    digest: str
    state_profile: str
    entry: str
    nodes: tuple[ProcessNode, ...]
    transitions: tuple[ProcessTransition, ...]


class ProcessChangeImpact(DomainModel):
    workflow_name: str
    from_revision: int
    to_revision: int
    added_nodes: tuple[str, ...] = ()
    removed_nodes: tuple[str, ...] = ()
    changed_nodes: tuple[str, ...] = ()
    missing_resume_mappings: tuple[str, ...] = ()
    compatible_for_in_flight: bool
    notes: tuple[str, ...] = Field(default_factory=tuple)


def build_process_manifest(definition: WorkflowDefinition) -> ProcessManifest:
    """Build an inspectable view from the same definition used by the runtime compiler."""
    from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler

    DeclarativeWorkflowCompiler(definition).validate()
    profile = get_state_profile(definition.spec.state)
    nodes = []
    transitions = []
    for name, step in definition.spec.steps.items():
        binding = profile.station_bindings.get(name)
        kind = (
            ProcessNodeKind(step.kind)
            if step.kind
            else ProcessNodeKind.GATE
            if name in profile.pause_nodes
            else ProcessNodeKind.STATION
            if binding
            else ProcessNodeKind.OPERATION
        )
        nodes.append(
            ProcessNode(
                name=name,
                kind=kind,
                station_contract=binding[0] if binding else None,
                station_contract_version=binding[1] if binding else None,
                required_policies=step.required_policies,
                allowed_effects=step.allowed_effects,
                join=step.join,
                max_concurrency=step.max_concurrency,
                retry_bound=step.retry_bound,
            )
        )
        if step.next:
            transitions.append(ProcessTransition(source=name, target=step.next))
        elif step.dynamic_route:
            transitions.extend(
                ProcessTransition(source=name, target=target, outcome="dynamic")
                for target in step.dynamic_targets
            )
        else:
            transitions.extend(
                ProcessTransition(source=name, target=target, outcome=outcome)
                for outcome, target in step.branches.items()
            )
    return ProcessManifest(
        workflow_name=definition.metadata.name,
        revision=definition.metadata.revision,
        digest=definition.digest,
        state_profile=definition.spec.state,
        entry=definition.spec.entry,
        nodes=tuple(nodes),
        transitions=tuple(transitions),
    )


def render_mermaid(manifest: ProcessManifest) -> str:
    """Render a deterministic flowchart from the canonical manifest."""
    lines = ["flowchart TD", f"    __start__([start]) --> {manifest.entry}"]
    for node in manifest.nodes:
        if node.kind is ProcessNodeKind.GATE:
            lines.append(f'    {node.name}{{"{node.name}"}}')
        elif node.kind is ProcessNodeKind.STATION:
            lines.append(f'    {node.name}["{node.name}\\n{node.station_contract}"]')
        else:
            lines.append(f'    {node.name}["{node.name}"]')
    lines.append("    __end__([end])")
    for transition in manifest.transitions:
        label = f"|{transition.outcome}|" if transition.outcome else ""
        lines.append(f"    {transition.source} -->{label} {transition.target}")
    return "\n".join(lines)


def compare_process_definitions(
    previous: WorkflowDefinition, current: WorkflowDefinition
) -> ProcessChangeImpact:
    """Report structural and in-flight compatibility impact before publication."""
    if previous.metadata.name != current.metadata.name:
        raise ValueError("Cannot compare definitions with different workflow names")
    old = previous.spec.steps
    new = current.spec.steps
    added = tuple(sorted(set(new) - set(old)))
    removed = tuple(sorted(set(old) - set(new)))
    changed = tuple(sorted(name for name in set(old) & set(new) if old[name] != new[name]))
    mappings = current.spec.resume.from_revisions.get(previous.metadata.revision, {})
    missing = tuple(sorted(name for name in removed if name not in mappings))
    notes = []
    if (
        current.metadata.revision <= previous.metadata.revision
        and current.digest != previous.digest
    ):
        notes.append("changed content must increment metadata.revision")
    if previous.spec.state != current.spec.state:
        notes.append("state profile changes cannot migrate in-flight instances")
    if current.spec.entry != previous.spec.entry:
        notes.append("entry changed; this affects new instances only")
    compatible = (
        not missing
        and previous.spec.state == current.spec.state
        and not any("must increment" in note for note in notes)
    )
    return ProcessChangeImpact(
        workflow_name=current.metadata.name,
        from_revision=previous.metadata.revision,
        to_revision=current.metadata.revision,
        added_nodes=added,
        removed_nodes=removed,
        changed_nodes=changed,
        missing_resume_mappings=missing,
        compatible_for_in_flight=compatible,
        notes=tuple(notes),
    )
