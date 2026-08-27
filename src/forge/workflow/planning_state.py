"""Checkpoint-safe helpers for layered planning artifacts and executable work."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from forge.workflow.base import ArtifactRef, WorkUnit


def content_digest(content: str) -> str:
    """Return the stable digest used for artifact approval and invalidation."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def record_planning_artifact(
    state: Mapping[str, Any], kind: str, content: str
) -> dict[str, Any]:
    """Persist generated planning content in the authoritative artifact lineage."""
    normalized = content.strip()
    digest = content_digest(normalized)
    existing = next(
        (item for item in planning_artifacts(state) if item.get("kind") == kind),
        None,
    )
    parent = next(
        (
            item
            for item in reversed(planning_artifacts(state))
            if item.get("kind") in {"prd", "spec", "rca", "plan"}
            and item.get("kind") != kind
        ),
        None,
    )
    artifact: ArtifactRef = {
        "id": (
            str(existing.get("id"))
            if existing
            else f"artifact:{state.get('ticket_key') or state.get('thread_id') or 'unknown'}:{kind}"
        ),
        "kind": kind,
        "source": "station",
        "content": normalized,
        "digest": digest,
        "approved_digest": digest,
        "status": "approved",
        "revision": int(existing.get("revision") or 0) + 1 if existing else 1,
        "repository": existing.get("repository") if existing else None,
        "input_artifact_ids": [parent["id"]] if parent and parent.get("id") else [],
        "parent_artifact_id": parent.get("id") if parent else None,
        "child_artifact_ids": list(existing.get("child_artifact_ids") or []) if existing else [],
        "provenance": {"station": "artifact-generation", "schema_version": "1.0"},
    }
    return apply_artifact_update(state, artifact)


def artifact_is_current(artifact: Mapping[str, Any]) -> bool:
    """Return whether an artifact may participate in work resolution.

    Lifecycle state and the digest-bound approval are both required.
    """
    status = artifact.get("status")
    if status != "approved":
        return False
    digest = artifact.get("digest")
    approved_digest = artifact.get("approved_digest")
    return bool(digest and approved_digest == digest)


def planning_artifacts(state: Mapping[str, Any]) -> list[ArtifactRef]:
    """Return the authoritative normalized artifact lineage."""
    return [cast(ArtifactRef, dict(item)) for item in state.get("artifacts") or []]


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
