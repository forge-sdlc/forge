from datetime import UTC, datetime, timedelta

from forge.workflow.checkpoint_migration import migrate_unpinned_checkpoint
from forge.workflow.declarative.builtins import builtin_feature_definition

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def legacy_state(**updates):
    state = {
        "thread_id": "PROJ-1",
        "workflow_name": "feature",
        "current_node": "generate_prd",
        "prd_content": "# Requirements",
        "current_repo": "acme/api",
        "repos_to_process": ["acme/api"],
        "pull_requests": {
            "acme/api": {
                "repo": "acme/api",
                "number": 42,
                "url": "https://example.test/acme/api/pull/42",
            }
        },
    }
    state.update(updates)
    return state


def test_dry_run_reports_compatibility_without_mutating_checkpoint() -> None:
    state = legacy_state()

    report = migrate_unpinned_checkpoint(state, builtin_feature_definition(), apply=False, now=NOW)

    assert report.compatible
    assert not report.applied
    assert report.migrated_state is None
    assert state["pull_requests"] == {"acme/api": state["pull_requests"]["acme/api"]}


def test_apply_normalizes_and_pins_checkpoint_with_rollback_deadline() -> None:
    report = migrate_unpinned_checkpoint(
        legacy_state(),
        builtin_feature_definition(),
        apply=True,
        now=NOW,
        rollback_window=timedelta(days=3),
    )

    assert report.applied
    assert report.rollback_until == NOW + timedelta(days=3)
    migrated = report.migrated_state
    assert migrated is not None
    assert migrated["workflow_pin_status"] == "phase8_migrated"
    assert migrated["workflow_definition_digest"] == builtin_feature_definition().digest
    assert migrated["current_repository"] == "acme/api"
    assert "acme/api:42" in migrated["pull_requests"]
    assert migrated["artifacts"][0]["kind"] == "prd"
    assert migrated["capabilities"]["repositories_resolved"] is True
    assert migrated["workflow_rollback_until"] == "2026-08-31T00:00:00+00:00"


def test_incompatible_position_is_rejected_without_migration() -> None:
    report = migrate_unpinned_checkpoint(
        legacy_state(current_node="deleted_node"),
        builtin_feature_definition(),
        apply=True,
        now=NOW,
    )

    assert not report.compatible
    assert not report.applied
    assert report.migrated_state is None
    assert report.reasons == (
        "current position 'deleted_node' does not exist in target definition",
    )


def test_already_pinned_checkpoint_is_never_rewritten() -> None:
    report = migrate_unpinned_checkpoint(
        legacy_state(workflow_definition_revision=1),
        builtin_feature_definition(),
        apply=True,
        now=NOW,
    )

    assert not report.compatible
    assert report.reasons == ("checkpoint is already definition-pinned",)
