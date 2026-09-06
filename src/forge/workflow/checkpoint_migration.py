"""Explicit Phase 8 migration for checkpoints created before definition pinning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from forge.domain import JsonValue, VersionedDomainModel
from forge.workflow.declarative.models import WorkflowDefinition
from forge.workflow.planning_state import record_planning_artifact
from forge.workflow.preconditions import project_capabilities


class CheckpointMigrationReport(VersionedDomainModel):
    """Dry-run/apply evidence for one immutable checkpoint migration."""

    run_id: str
    workflow_name: str
    source_status: str
    target_revision: int
    target_digest: str
    compatible: bool
    applied: bool = False
    reasons: tuple[str, ...] = ()
    rollback_until: datetime | None = None
    migrated_state: dict[str, JsonValue] | None = Field(default=None, exclude=True)


def migrate_unpinned_checkpoint(
    state: dict[str, Any],
    definition: WorkflowDefinition,
    *,
    apply: bool = False,
    now: datetime | None = None,
    rollback_window: timedelta = timedelta(days=7),
) -> CheckpointMigrationReport:
    """Validate and optionally pin a legacy checkpoint to one explicit artifact.

    The function is pure: callers persist the returned state only after storing
    their own checkpoint backup. Normal workflow resume never invokes it.
    """
    now = now or datetime.now(UTC)
    run_id = str(state.get("thread_id") or state.get("ticket_key") or "unknown")
    workflow_name = str(state.get("workflow_name") or "")
    reasons: list[str] = []
    if not workflow_name:
        reasons.append("checkpoint has no workflow identity")
    elif workflow_name != definition.metadata.name:
        reasons.append("checkpoint workflow does not match target definition")
    if state.get("workflow_definition_revision") or state.get("workflow_definition_digest"):
        reasons.append("checkpoint is already definition-pinned")
    position = str(state.get("current_node") or "entry")
    valid_positions = set(definition.spec.steps) | {"entry", "complete", "__end__"}
    if position not in valid_positions:
        reasons.append(f"current position {position!r} does not exist in target definition")

    compatible = not reasons
    migrated: dict[str, JsonValue] | None = None
    rollback_until = now + rollback_window if compatible else None
    if compatible and apply:
        assert rollback_until is not None
        normalized: dict[str, Any] = dict(state)
        for kind, field in (
            ("prd", "prd_content"),
            ("spec", "spec_content"),
            ("rca", "rca_content"),
            ("plan", "plan_content"),
        ):
            content = normalized.get(field)
            if (
                isinstance(content, str)
                and content.strip()
                and not any(item.get("kind") == kind for item in normalized.get("artifacts") or [])
            ):
                normalized.update(record_planning_artifact(normalized, kind, content))
        repositories = list(normalized.get("repositories") or [])
        known = {item.get("name") for item in repositories}
        for name in [
            normalized.get("current_repo"),
            *(normalized.get("repos_to_process") or []),
            *((normalized.get("tasks_by_repo") or {}).keys()),
        ]:
            if isinstance(name, str) and name and name not in known:
                repositories.append(
                    {
                        "name": name,
                        "source": "checkpoint_migration",
                        "status": (
                            "completed"
                            if name in set(normalized.get("repos_completed") or [])
                            else "pending"
                        ),
                        "work_unit_ids": list(
                            (normalized.get("tasks_by_repo") or {}).get(name, [])
                        ),
                    }
                )
                known.add(name)
        normalized["repositories"] = repositories
        normalized["current_repository"] = normalized.get("current_repository") or normalized.get(
            "current_repo"
        )
        pull_requests: dict[str, Any] = {}
        for key, record in (normalized.get("pull_requests") or {}).items():
            target = key
            if ":" not in str(key) and isinstance(record, dict):
                number = record.get("number")
                url = record.get("url")
                if number is not None:
                    target = f"{key}:{number}"
                elif url:
                    target = f"{key}:{url}"
            pull_requests[str(target)] = record
        normalized["pull_requests"] = pull_requests
        migrated = {
            **normalized,
            "workflow_name": definition.metadata.name,
            "workflow_revision": definition.metadata.revision,
            "workflow_digest": definition.digest,
            "workflow_definition_revision": definition.metadata.revision,
            "workflow_definition_digest": definition.digest,
            "workflow_definition": definition.canonical_dict(),
            "workflow_pin_status": "phase8_migrated",
            "workflow_state_profile": definition.spec.state,
            "workflow_migrated_at": now.isoformat(),
            "workflow_rollback_until": rollback_until.isoformat(),
        }
        migrated["capabilities"] = project_capabilities(migrated)
    return CheckpointMigrationReport(
        run_id=run_id,
        workflow_name=workflow_name or "unidentified",
        source_status="legacy_unpinned",
        target_revision=definition.metadata.revision,
        target_digest=definition.digest,
        compatible=compatible,
        applied=bool(compatible and apply),
        reasons=tuple(reasons),
        rollback_until=rollback_until,
        migrated_state=migrated,
    )


__all__ = ["CheckpointMigrationReport", "migrate_unpinned_checkpoint"]
