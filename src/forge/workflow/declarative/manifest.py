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
    external_entry: bool = False


class ProcessManifest(DomainModel):
    workflow_name: str
    revision: int
    digest: str
    state_profile: str
    entry: str
    nodes: tuple[ProcessNode, ...]
    transitions: tuple[ProcessTransition, ...]


class ProcessChangeClassification(StrEnum):
    """Compatibility class assigned to a process-definition revision change.

    The names intentionally mirror the governance vocabulary.  In particular,
    ``compatible`` does not mean that an active checkpoint may silently switch
    topology: :func:`compare_process_definitions` still requires an explicit
    mapping for changes that can affect a checkpoint.
    """

    PATCH = "patch"
    COMPATIBLE = "compatible"
    MIGRATABLE = "migratable"
    BREAKING = "breaking"


# A few callers use the shorter terminology from the governance document.
ChangeClassification = ProcessChangeClassification
ProcessCompatibilityClass = ProcessChangeClassification


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
    classification: ProcessChangeClassification = ProcessChangeClassification.PATCH
    # These fields make the impact report useful to release tooling without
    # making consumers parse free-form notes.  Names are node names unless
    # otherwise stated, and all values are stable and sorted.
    changed_transitions: tuple[str, ...] = ()
    routing_changes: tuple[str, ...] = ()
    outcome_changes: tuple[str, ...] = ()
    station_contract_changes: tuple[str, ...] = ()
    effect_capability_changes: tuple[str, ...] = ()
    policy_changes: tuple[str, ...] = ()
    join_changes: tuple[str, ...] = ()
    concurrency_changes: tuple[str, ...] = ()
    retry_changes: tuple[str, ...] = ()
    state_profile_changed: bool = False
    entry_changed: bool = False
    same_revision_mutation: bool = False

    @property
    def compatibility_class(self) -> ProcessChangeClassification:
        """Alias used by governance/reporting clients."""
        return self.classification


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
                required_policies=tuple(sorted(step.required_policies)),
                allowed_effects=tuple(sorted(step.allowed_effects)),
                join=step.join,
                max_concurrency=step.max_concurrency,
                retry_bound=step.retry_bound,
                external_entry=step.external_entry,
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
    # Mappings are semantically unordered.  Keep inspection and rendering
    # stable when equivalent definitions use a different source ordering.
    nodes.sort(key=lambda node: node.name)
    transitions.sort(key=lambda edge: (edge.source, edge.target, edge.outcome or ""))
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
    for node in sorted(manifest.nodes, key=lambda item: item.name):
        if node.kind is ProcessNodeKind.GATE:
            lines.append(f'    {node.name}{{"{node.name}"}}')
        elif node.kind is ProcessNodeKind.STATION:
            lines.append(f'    {node.name}["{node.name}\\n{node.station_contract}"]')
        else:
            lines.append(f'    {node.name}["{node.name}"]')
    lines.append("    __end__([end])")
    for transition in sorted(
        manifest.transitions,
        key=lambda edge: (edge.source, edge.target, edge.outcome or ""),
    ):
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
    old_names = set(old)
    new_names = set(new)
    added = tuple(sorted(new_names - old_names))
    removed = tuple(sorted(old_names - new_names))

    def step_signature(step: Any) -> tuple[Any, ...]:
        """Executable step fields, normalizing fields whose order is irrelevant."""
        return (
            step.next,
            step.route,
            tuple(sorted(step.branches.items())),
            step.dynamic_route,
            tuple(sorted(step.dynamic_targets)),
            step.kind,
            step.station_contract,
            step.station_contract_version,
            tuple(sorted(step.required_policies)),
            tuple(sorted(step.allowed_effects)),
            step.join,
            step.max_concurrency,
            step.retry_bound,
        )

    common = old_names & new_names
    changed = tuple(
        sorted(name for name in common if step_signature(old[name]) != step_signature(new[name]))
    )

    def transitions(steps: Mapping[str, Any]) -> dict[str, frozenset[tuple[str, str, str | None]]]:
        result: dict[str, frozenset[tuple[str, str, str | None]]] = {}
        for name, step in steps.items():
            edges: set[tuple[str, str, str | None]]
            if step.next:
                edges = {(name, step.next, None)}
            elif step.dynamic_route:
                edges = {(name, target, "dynamic") for target in step.dynamic_targets}
            else:
                edges = {(name, target, outcome) for outcome, target in step.branches.items()}
            result[name] = frozenset(edges)
        return result

    old_transitions = transitions(old)
    new_transitions = transitions(new)
    changed_transitions = tuple(
        sorted(name for name in common if old_transitions[name] != new_transitions[name])
    )
    routing_changes: list[str] = []
    outcome_changes: list[str] = []
    for name in changed_transitions:
        old_edges = old_transitions[name]
        new_edges = new_transitions[name]
        old_outcomes = {outcome for _source, _target, outcome in old_edges}
        new_outcomes = {outcome for _source, _target, outcome in new_edges}
        if old_outcomes != new_outcomes:
            outcome_changes.append(name)
        if old_edges != new_edges:
            routing_changes.append(name)

    station_contract_changes = tuple(
        sorted(
            name
            for name in common
            if (old[name].station_contract, old[name].station_contract_version)
            != (new[name].station_contract, new[name].station_contract_version)
        )
    )
    effect_capability_changes = tuple(
        sorted(
            name
            for name in common
            if set(old[name].allowed_effects) != set(new[name].allowed_effects)
        )
    )
    policy_changes = tuple(
        sorted(
            name
            for name in common
            if set(old[name].required_policies) != set(new[name].required_policies)
        )
    )
    if set(previous.spec.mandatory_policies) != set(current.spec.mandatory_policies):
        policy_changes = tuple(sorted(set(policy_changes) | {"<workflow>"}))
    if set(previous.spec.extension_points) != set(current.spec.extension_points):
        policy_changes = tuple(sorted(set(policy_changes) | {"<extensions>"}))
    join_changes = tuple(sorted(name for name in common if old[name].join != new[name].join))
    concurrency_changes = tuple(
        sorted(name for name in common if old[name].max_concurrency != new[name].max_concurrency)
    )
    retry_changes = tuple(
        sorted(name for name in common if old[name].retry_bound != new[name].retry_bound)
    )

    mappings = current.spec.resume.from_revisions.get(previous.metadata.revision, {})
    missing = tuple(sorted(name for name in removed if name not in mappings))
    notes: list[str] = []
    state_profile_changed = previous.spec.state != current.spec.state
    entry_changed = current.spec.entry != previous.spec.entry
    same_revision_mutation = (
        current.metadata.revision == previous.metadata.revision
        and current.digest != previous.digest
    )
    rollback = current.metadata.revision < previous.metadata.revision
    if same_revision_mutation:
        notes.append("changed content must increment metadata.revision")
        notes.append("same revision has different content (immutable revision mutation)")
    if rollback:
        notes.append("target revision is older than the source revision")
    if state_profile_changed:
        notes.append("state profile changes cannot migrate in-flight instances")
    if entry_changed:
        notes.append("entry changed; this affects new instances only")
    if added:
        notes.append(f"added nodes: {', '.join(added)}")
    if removed:
        notes.append(f"removed nodes: {', '.join(removed)}")
    if missing:
        notes.append(f"missing resume mappings: {', '.join(missing)}")
    if routing_changes:
        notes.append(f"routing changed on: {', '.join(routing_changes)}")
    if outcome_changes:
        notes.append(f"outcomes changed on: {', '.join(outcome_changes)}")
    if station_contract_changes:
        notes.append(f"station contract/version changed on: {', '.join(station_contract_changes)}")
    if effect_capability_changes:
        notes.append(f"effect capabilities changed on: {', '.join(effect_capability_changes)}")
    if policy_changes:
        notes.append(f"policies changed on: {', '.join(policy_changes)}")
    if join_changes:
        notes.append(f"join semantics changed on: {', '.join(join_changes)}")
    if concurrency_changes:
        notes.append(f"concurrency changed on: {', '.join(concurrency_changes)}")
    if retry_changes:
        notes.append(f"retry policy changed on: {', '.join(retry_changes)}")

    # Fail closed for anything which can alter the meaning of a checkpoint.
    severe = (
        same_revision_mutation
        or rollback
        or state_profile_changed
        or bool(station_contract_changes)
        or bool(effect_capability_changes)
        or bool(policy_changes)
        or bool(join_changes)
        or bool(concurrency_changes)
        or bool(retry_changes)
    )
    removed_mapped = bool(removed) and not missing
    if severe or missing:
        classification = ProcessChangeClassification.BREAKING
    elif removed_mapped:
        classification = ProcessChangeClassification.MIGRATABLE
    elif routing_changes:
        # Retained outcomes are safe for newly-created instances, but there is
        # no implicit checkpoint conversion for already-running instances.
        old_outcome_removed = any(
            {outcome for _s, _t, outcome in old_transitions[name]}
            - {outcome for _s, _t, outcome in new_transitions[name]}
            for name in changed_transitions
        )
        only_additive_outcomes = all(
            old_transitions[name] <= new_transitions[name] for name in changed_transitions
        )
        classification = (
            ProcessChangeClassification.BREAKING
            if old_outcome_removed
            else ProcessChangeClassification.COMPATIBLE
            if only_additive_outcomes
            else ProcessChangeClassification.MIGRATABLE
        )
    elif added or entry_changed:
        classification = ProcessChangeClassification.COMPATIBLE
    else:
        classification = ProcessChangeClassification.PATCH

    compatible = classification in {
        ProcessChangeClassification.PATCH,
        ProcessChangeClassification.COMPATIBLE,
    }
    if removed_mapped and not severe and not routing_changes:
        compatible = True
    if routing_changes or station_contract_changes or effect_capability_changes:
        compatible = False
    if state_profile_changed or same_revision_mutation or rollback or missing:
        compatible = False
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
        classification=classification,
        changed_transitions=changed_transitions,
        routing_changes=tuple(sorted(routing_changes)),
        outcome_changes=tuple(sorted(outcome_changes)),
        station_contract_changes=station_contract_changes,
        effect_capability_changes=effect_capability_changes,
        policy_changes=policy_changes,
        join_changes=join_changes,
        concurrency_changes=concurrency_changes,
        retry_changes=retry_changes,
        state_profile_changed=state_profile_changed,
        entry_changed=entry_changed,
        same_revision_mutation=same_revision_mutation,
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
