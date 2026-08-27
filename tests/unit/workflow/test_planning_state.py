"""Tests for normalized layered-planning state behavior."""

from forge.workflow.planning_state import (
    apply_artifact_update,
    artifact_is_current,
    planning_artifacts,
)


def artifact(
    artifact_id: str,
    kind: str,
    digest: str,
    *,
    children: list[str] | None = None,
) -> dict:
    return {
        "id": artifact_id,
        "kind": kind,
        "content": artifact_id,
        "digest": digest,
        "approved_digest": digest,
        "status": "approved",
        "child_artifact_ids": children or [],
    }


def test_approval_is_bound_to_the_current_digest() -> None:
    approved = artifact("spec:1", "spec", "sha256:one")
    changed = {**approved, "digest": "sha256:two"}

    assert artifact_is_current(approved) is True
    assert artifact_is_current(changed) is False
    assert artifact_is_current({"id": "unversioned", "kind": "spec"}) is False


def test_normalized_kind_prevents_duplicate_legacy_artifact() -> None:
    state = {
        "ticket_key": "FEAT-1",
        "spec_content": "legacy design",
        "artifacts": [artifact("spec:1", "spec", "sha256:normalized")],
    }

    assert [item["id"] for item in planning_artifacts(state)] == ["spec:1"]


def test_parent_revision_stales_all_descendants_and_pending_work() -> None:
    prd = artifact("prd:1", "prd", "sha256:old", children=["spec:1"])
    spec = artifact("spec:1", "spec", "sha256:spec", children=["plan:1"])
    plan = artifact("plan:1", "plan", "sha256:plan", children=["task:1"])
    task = artifact("task:1", "task", "sha256:task")
    state = {
        "artifacts": [prd, spec, plan, task],
        "work_units": [
            {
                "id": "TASK-1",
                "kind": "task",
                "status": "pending",
                "source_artifact_ids": ["task:1"],
                "context_artifact_ids": ["plan:1", "spec:1", "prd:1"],
            },
            {
                "id": "TASK-0",
                "kind": "task",
                "status": "completed",
                "source_artifact_ids": ["task:1"],
            },
        ],
    }

    update = apply_artifact_update(
        state,
        {
            **prd,
            "content": "new requirements",
            "digest": "sha256:new",
            "approved_digest": None,
            "status": "draft",
            "revision": 2,
        },
    )

    status_by_id = {item["id"]: item["status"] for item in update["artifacts"]}
    assert status_by_id == {
        "prd:1": "draft",
        "spec:1": "stale",
        "plan:1": "stale",
        "task:1": "stale",
    }
    assert update["work_units"][0]["status"] == "stale"
    assert update["work_units"][1]["status"] == "completed"
