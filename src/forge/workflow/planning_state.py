"""Checkpoint-safe helpers for layered planning artifacts and executable work."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from forge.workflow.base import ArtifactRef, RepositoryRef, WorkUnit

LEGACY_ARTIFACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("prd", "prd_content"),
    ("spec", "spec_content"),
    ("rca", "rca_content"),
    ("plan", "plan_content"),
)


def content_digest(content: str) -> str:
    """Return the stable digest used for artifact approval and invalidation."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def artifact_is_current(artifact: Mapping[str, Any]) -> bool:
    """Return whether an artifact may participate in work resolution.

    Status-less artifacts are accepted for checkpoints created before lifecycle
    metadata existed. Explicit lifecycle state is authoritative.
    """
    status = artifact.get("status")
    if status is None:
        return True
    if status != "approved":
        return False
    digest = artifact.get("digest")
    approved_digest = artifact.get("approved_digest")
    return bool(digest and approved_digest == digest)


def legacy_artifacts(state: Mapping[str, Any]) -> list[ArtifactRef]:
    """Adapt legacy planning content fields into an ordered artifact lineage."""
    ticket_key = str(state.get("ticket_key") or "unknown")
    artifacts: list[ArtifactRef] = []
    previous_id: str | None = None
    for kind, field in LEGACY_ARTIFACT_FIELDS:
        content = state.get(field)
        if not isinstance(content, str) or not content.strip():
            continue
        normalized = content.strip()
        digest = content_digest(normalized)
        artifact_id = f"legacy:{ticket_key}:{kind}:{digest[7:19]}"
        artifact: ArtifactRef = {
            "id": artifact_id,
            "kind": kind,
            "source": field,
            "content": normalized,
            "digest": digest,
            "approved_digest": digest,
            "status": "approved",
            "revision": 1,
            "repository": None,
            "input_artifact_ids": [previous_id] if previous_id else [],
            "parent_artifact_id": previous_id,
            "child_artifact_ids": [],
            "provenance": {"adapter": "legacy_state", "field": field},
        }
        if artifacts:
            artifacts[-1].setdefault("child_artifact_ids", []).append(artifact_id)
        artifacts.append(artifact)
        previous_id = artifact_id
    return artifacts


def planning_artifacts(state: Mapping[str, Any]) -> list[ArtifactRef]:
    """Return normalized artifacts plus non-duplicated legacy compatibility input."""
    normalized = [cast(ArtifactRef, dict(item)) for item in state.get("artifacts") or []]
    normalized_kinds = {
        str(item.get("kind"))
        for item in normalized
        if item.get("kind") in {"prd", "spec", "rca", "plan"}
    }
    compatible = [
        item for item in legacy_artifacts(state) if item.get("kind") not in normalized_kinds
    ]
    return [*normalized, *compatible]


def repository_compatibility_update(
    state: Mapping[str, Any], *, current: str | None = None
) -> dict[str, Any]:
    """Synchronize normalized repository state with legacy traversal fields."""
    repository_items = [cast(RepositoryRef, dict(item)) for item in state.get("repositories") or []]
    names = [
        str(item["name"])
        for item in repository_items
        if isinstance(item.get("name"), str) and item.get("name")
    ]
    legacy_names = [
        *list(state.get("repos_to_process") or []),
        *list((state.get("tasks_by_repo") or {}).keys()),
    ]
    selected = current or state.get("current_repository") or state.get("current_repo")
    if isinstance(selected, str) and selected:
        legacy_names.insert(0, selected)
    for name in legacy_names:
        if isinstance(name, str) and name and name not in names:
            names.append(name)

    completed = set(state.get("repos_completed") or [])
    by_name = {item.get("name"): item for item in repository_items}
    repositories: list[RepositoryRef] = []
    for name in names:
        existing = by_name.get(name)
        repositories.append(
            existing
            or {
                "name": name,
                "source": "legacy_state",
                "status": "completed" if name in completed else "pending",
                "work_unit_ids": [],
            }
        )
    return {
        "repositories": repositories,
        "current_repository": selected,
        "current_repo": selected,
        "repos_to_process": names,
    }


def upsert_artifact(
    artifacts: Sequence[ArtifactRef], artifact: ArtifactRef
) -> tuple[list[ArtifactRef], set[str]]:
    """Upsert an artifact and stale its transitive descendants when content changes.

    Returns the updated collection and the IDs made stale. Completed work remains
    historical; callers use :func:`stale_dependent_work_units` for pending work.
    """
    artifact_id = artifact.get("id")
    if not artifact_id:
        raise ValueError("Artifact id is required")
    updated = [cast(ArtifactRef, dict(item)) for item in artifacts]
    by_id = {item.get("id"): index for index, item in enumerate(updated)}
    changed = False
    if artifact_id in by_id:
        previous = updated[by_id[artifact_id]]
        changed = previous.get("digest") != artifact.get("digest")
        updated[by_id[artifact_id]] = cast(ArtifactRef, {**previous, **artifact})
    else:
        updated.append(cast(ArtifactRef, dict(artifact)))

    stale_ids: set[str] = set()
    if changed:
        queue = deque(
            child_id
            for item in artifacts
            if item.get("id") == artifact_id
            for child_id in item.get("child_artifact_ids") or []
        )
        while queue:
            child_id = queue.popleft()
            if child_id in stale_ids:
                continue
            stale_ids.add(child_id)
            child_index = by_id.get(child_id)
            if child_index is None:
                continue
            child = updated[child_index]
            child["status"] = "stale"
            child["approved"] = False
            queue.extend(child.get("child_artifact_ids") or [])
    return updated, stale_ids


def stale_dependent_work_units(
    work_units: Sequence[WorkUnit], stale_artifact_ids: Iterable[str]
) -> list[WorkUnit]:
    """Mark pending/active work derived from stale artifacts as stale."""
    stale = set(stale_artifact_ids)
    result: list[WorkUnit] = []
    for original in work_units:
        unit = cast(WorkUnit, dict(original))
        dependencies = set(unit.get("source_artifact_ids") or []) | set(
            unit.get("context_artifact_ids") or []
        )
        if unit.get("status") != "completed" and dependencies & stale:
            unit["status"] = "stale"
        result.append(unit)
    return result


def apply_artifact_update(state: Mapping[str, Any], artifact: ArtifactRef) -> dict[str, Any]:
    """Apply an artifact revision and propagate staleness in one state update."""
    artifacts, stale_ids = upsert_artifact(planning_artifacts(state), artifact)
    work_units = stale_dependent_work_units(state.get("work_units") or [], stale_ids)
    return {"artifacts": artifacts, "work_units": work_units}
