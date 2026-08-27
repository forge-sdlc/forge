"""Runtime-independent inspection and revision impact for workflow definitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

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


class ProcessMigrationClassification(StrEnum):
    """Eligibility of an active instance for a definition revision change."""

    STAYS_PINNED = "stays_pinned"
    CAN_ADOPT_DIRECTLY = "can_adopt_directly"
    REQUIRES_RESUME_MAPPING = "requires_resume_mapping"
    BLOCKED = "blocked"


class ProcessInstanceSnapshot(DomainModel):
    """The immutable metadata needed to dry-run one active checkpoint.

    Runtime checkpoints historically used ``workflow_revision`` and
    ``workflow_digest``.  The simulator deliberately calls these values
    ``pinned_*`` to make it clear that they are instance-owned, not the
    currently activated definition.
    """

    run_id: str | None = None
    thread_id: str | None = None
    instance_id: str | None = None
    current_node: str | None = None
    pinned_revision: int | None = None
    pinned_digest: str | None = None
    state_profile: str | None = None


class ProcessMigrationInstanceResult(DomainModel):
    """Deterministic result for one active workflow instance."""

    identity: str
    run_id: str | None = None
    thread_id: str | None = None
    instance_id: str | None = None
    current_node: str | None = None
    pinned_revision: int | None = None
    pinned_digest: str | None = None
    classification: ProcessMigrationClassification
    eligible: bool
    target_revision: int | None = None
    target_node: str | None = None
    reason_code: str
    reason: str

    @property
    def status(self) -> ProcessMigrationClassification:
        """Compatibility alias for consumers that call the class a status."""
        return self.classification


class ProcessMigrationSimulation(DomainModel):
    """Aggregate, deterministic dry-run report for active instances."""

    workflow_name: str
    from_revision: int
    to_revision: int
    from_digest: str
    to_digest: str
    instances: tuple[ProcessMigrationInstanceResult, ...]
    counts: dict[str, int]
    compatible: bool
    invalid_resume_mappings: tuple[str, ...] = ()

    @property
    def details(self) -> tuple[ProcessMigrationInstanceResult, ...]:
        """Alias useful to callers that consume reports as ``details``."""
        return self.instances

    @property
    def results(self) -> tuple[ProcessMigrationInstanceResult, ...]:
        return self.instances

    @property
    def by_classification(self) -> dict[str, int]:
        return dict(self.counts)

    @property
    def total_count(self) -> int:
        return len(self.instances)

    @property
    def blocked_count(self) -> int:
        return self.counts[ProcessMigrationClassification.BLOCKED.value]

    @property
    def stays_pinned_count(self) -> int:
        return self.counts[ProcessMigrationClassification.STAYS_PINNED.value]

    @property
    def can_adopt_directly_count(self) -> int:
        return self.counts[ProcessMigrationClassification.CAN_ADOPT_DIRECTLY.value]

    @property
    def requires_resume_mapping_count(self) -> int:
        return self.counts[ProcessMigrationClassification.REQUIRES_RESUME_MAPPING.value]

    @property
    def eligible_count(self) -> int:
        return sum(value for key, value in self.counts.items() if key != "blocked")


# Short aliases keep the public API pleasant while retaining the explicit
# ``Process*`` names used by the manifest and change-impact models.
MigrationClassification = ProcessMigrationClassification
MigrationInstanceResult = ProcessMigrationInstanceResult
MigrationSimulationResult = ProcessMigrationSimulation
ActiveInstanceSnapshot = ProcessInstanceSnapshot
ProcessMigrationStatus = ProcessMigrationClassification
MigrationStatus = ProcessMigrationClassification


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


_CONTROL_NODES = frozenset({"", "start", "entry", "__end__", "complete"})
_SNAPSHOT_KEYS: dict[str, tuple[str, ...]] = {
    "run_id": ("run_id", "runId", "run"),
    "thread_id": ("thread_id", "threadId", "thread"),
    "instance_id": ("instance_id", "instanceId", "id"),
    "current_node": ("current_node", "currentNode", "node"),
    "pinned_revision": (
        "pinned_revision",
        "pinnedRevision",
        "workflow_revision",
        "workflowRevision",
        "revision",
    ),
    "pinned_digest": (
        "pinned_digest",
        "pinnedDigest",
        "workflow_digest",
        "workflowDigest",
        "digest",
    ),
    "state_profile": (
        "state_profile",
        "stateProfile",
        "workflow_state_profile",
        "workflowStateProfile",
    ),
}


def _snapshot_field(snapshot: Mapping[str, Any], name: str) -> Any:
    for key in _SNAPSHOT_KEYS[name]:
        if key in snapshot:
            return snapshot[key]
    return None


def _coerce_snapshot(value: ProcessInstanceSnapshot | Mapping[str, Any]) -> ProcessInstanceSnapshot:
    if isinstance(value, ProcessInstanceSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("active instances must be mappings or ProcessInstanceSnapshot values")
    raw_revision = _snapshot_field(value, "pinned_revision")
    try:
        revision = int(raw_revision) if raw_revision is not None else None
    except (TypeError, ValueError):
        revision = None
    def as_text(field: str) -> str | None:
        raw = _snapshot_field(value, field)
        return str(raw) if raw is not None else None

    return ProcessInstanceSnapshot(
        run_id=as_text("run_id"),
        thread_id=as_text("thread_id"),
        instance_id=as_text("instance_id"),
        current_node=as_text("current_node"),
        pinned_revision=revision,
        pinned_digest=as_text("pinned_digest"),
        state_profile=as_text("state_profile"),
    )


def _instance_identity(snapshot: ProcessInstanceSnapshot) -> str:
    """Build an identity that remains stable when input order changes."""
    if snapshot.instance_id:
        return snapshot.instance_id
    parts = []
    if snapshot.run_id:
        parts.append(f"run:{snapshot.run_id}")
    if snapshot.thread_id:
        parts.append(f"thread:{snapshot.thread_id}")
    return "/".join(parts) or "anonymous"


def _invalid_mapping_entries(definition: WorkflowDefinition) -> tuple[str, ...]:
    """Return invalid mapping entries in a stable, human-readable format."""
    invalid: list[str] = []
    for source_revision, mappings in definition.spec.resume.from_revisions.items():
        for source, target in mappings.items():
            if target not in definition.spec.steps:
                invalid.append(f"{source_revision}:{source}->{target}")
    return tuple(sorted(invalid))


def _migration_result(
    snapshot: ProcessInstanceSnapshot,
    *,
    classification: ProcessMigrationClassification,
    eligible: bool,
    reason_code: str,
    reason: str,
    target_revision: int | None = None,
    target_node: str | None = None,
) -> ProcessMigrationInstanceResult:
    return ProcessMigrationInstanceResult(
        identity=_instance_identity(snapshot),
        run_id=snapshot.run_id,
        thread_id=snapshot.thread_id,
        instance_id=snapshot.instance_id,
        current_node=snapshot.current_node,
        pinned_revision=snapshot.pinned_revision,
        pinned_digest=snapshot.pinned_digest,
        classification=classification,
        eligible=eligible,
        target_revision=target_revision,
        target_node=target_node,
        reason_code=reason_code,
        reason=reason,
    )


def simulate_process_migration(
    previous: WorkflowDefinition,
    current: WorkflowDefinition,
    active_instances: Iterable[ProcessInstanceSnapshot | Mapping[str, Any]],
) -> ProcessMigrationSimulation:
    """Dry-run adoption of ``current`` by active instances pinned to ``previous``.

    A simulation never mutates checkpoints.  An instance can be adopted directly
    when its saved node still exists in the new definition; a removed node needs
    an explicit mapping in the new immutable artifact.  Every mismatch in pinned
    identity is reported as blocked so operators can distinguish an unsafe source
    snapshot from a merely unmapped node.
    """
    impact = compare_process_definitions(previous, current)
    invalid_mappings = _invalid_mapping_entries(current)
    old_revision = previous.metadata.revision
    new_revision = current.metadata.revision
    mappings = current.spec.resume.from_revisions.get(old_revision, {})
    results: list[ProcessMigrationInstanceResult] = []

    for item in active_instances:
        snapshot = _coerce_snapshot(item)
        revision = snapshot.pinned_revision
        digest = snapshot.pinned_digest
        node = snapshot.current_node

        if revision is None or digest is None or node is None:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="incomplete_snapshot",
                    reason="active instance is missing current_node, pinned revision, or pinned digest",
                )
            )
            continue
        if snapshot.state_profile and snapshot.state_profile != previous.spec.state:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="state_profile_incompatible",
                    reason="pinned state profile does not match the source definition",
                )
            )
            continue
        if previous.spec.state != current.spec.state:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="state_profile_incompatible",
                    reason="source and target definitions use incompatible state profiles",
                )
            )
            continue
        if revision == new_revision:
            if digest != current.digest:
                results.append(
                    _migration_result(
                        snapshot,
                        classification=ProcessMigrationClassification.BLOCKED,
                        eligible=False,
                        reason_code="same_revision_digest_mutation",
                        reason="pinned revision has a different digest than the target artifact",
                    )
                )
            else:
                results.append(
                    _migration_result(
                        snapshot,
                        classification=ProcessMigrationClassification.STAYS_PINNED,
                        eligible=True,
                        reason_code="already_on_target",
                        reason="instance is already pinned to the target artifact",
                        target_revision=new_revision,
                        target_node=node,
                    )
                )
            continue
        if revision != old_revision or digest != previous.digest:
            code = "wrong_source_revision" if revision != old_revision else "wrong_source_digest"
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code=code,
                    reason="pinned artifact does not match the source definition being simulated",
                )
            )
            continue
        if new_revision <= old_revision:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="revision_rollback",
                    reason="target revision is not newer than the pinned source revision",
                )
            )
            continue

        if node in _CONTROL_NODES or node in current.spec.steps:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.CAN_ADOPT_DIRECTLY,
                    eligible=True,
                    reason_code="node_preserved",
                    reason="saved node exists in the target definition",
                    target_revision=new_revision,
                    target_node=node,
                )
            )
            continue
        if node not in mappings:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="removed_node_without_mapping",
                    reason="saved node was removed and has no declared resume mapping",
                )
            )
            continue
        target = mappings[node]
        if target not in current.spec.steps:
            results.append(
                _migration_result(
                    snapshot,
                    classification=ProcessMigrationClassification.BLOCKED,
                    eligible=False,
                    reason_code="invalid_mapping_target",
                    reason="declared resume mapping targets an undeclared node",
                )
            )
            continue
        results.append(
            _migration_result(
                snapshot,
                classification=ProcessMigrationClassification.REQUIRES_RESUME_MAPPING,
                eligible=True,
                reason_code="declared_resume_mapping",
                reason="saved node requires the declared resume mapping",
                target_revision=new_revision,
                target_node=target,
            )
        )

    results.sort(key=lambda result: (result.identity, result.run_id or "", result.thread_id or ""))
    counts = {classification.value: 0 for classification in ProcessMigrationClassification}
    for result in results:
        counts[result.classification.value] += 1
    return ProcessMigrationSimulation(
        workflow_name=current.metadata.name,
        from_revision=old_revision,
        to_revision=new_revision,
        from_digest=previous.digest,
        to_digest=current.digest,
        instances=tuple(results),
        counts=counts,
        compatible=not counts[ProcessMigrationClassification.BLOCKED.value]
        and impact.compatible_for_in_flight,
        invalid_resume_mappings=invalid_mappings,
    )


# Explicitly named alias for callers that use the report terminology.
simulate_process_definition_migration = simulate_process_migration
simulate_migration = simulate_process_migration
