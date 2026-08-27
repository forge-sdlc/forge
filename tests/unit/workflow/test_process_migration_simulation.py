from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import (
    ProcessMigrationClassification,
    simulate_process_migration,
)


def definition(*, revision: int, steps: dict, state: str = "feature", resume: dict | None = None):
    return load_workflow_value(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "migration-test", "revision": revision},
            "spec": {
                "state": state,
                "entry": next(iter(steps)),
                "steps": steps,
        **(
            {"resume": {"fromRevisions": {1: resume}}}
            if resume is not None
            else {}
        ),
            },
        }
    )


def test_simulation_classifies_direct_mapped_and_pinned_instances_deterministically():
    previous = definition(revision=1, steps={"old": {"next": "kept"}, "kept": {"next": "__end__"}})
    current = definition(
        revision=2,
        steps={"renamed": {"next": "kept"}, "kept": {"next": "__end__"}},
        resume={"old": "renamed"},
    )
    report = simulate_process_migration(
        previous,
        current,
        [
            {"run_id": "run-mapped", "thread_id": "b", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest},
            {"run_id": "run-direct", "thread_id": "a", "current_node": "kept", "workflow_revision": 1, "workflow_digest": previous.digest},
            {"run_id": "run-pinned", "thread_id": "c", "current_node": "kept", "workflow_revision": 2, "workflow_digest": current.digest},
        ],
    )

    assert [item.run_id for item in report.instances] == ["run-direct", "run-mapped", "run-pinned"]
    assert report.counts == {"stays_pinned": 1, "can_adopt_directly": 1, "requires_resume_mapping": 1, "blocked": 0}
    assert report.instances[1].classification is ProcessMigrationClassification.REQUIRES_RESUME_MAPPING
    assert report.instances[1].target_node == "renamed"
    assert report.compatible is True


def test_simulation_reports_identity_and_state_safety_failures():
    previous = definition(revision=1, steps={"old": {"next": "__end__"}})
    current = definition(revision=2, steps={"new": {"next": "__end__"}})
    report = simulate_process_migration(
        previous,
        current,
        [
            {"run_id": "mutated", "current_node": "old", "workflow_revision": 1, "workflow_digest": "wrong"},
            {"run_id": "profile", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest, "workflow_state_profile": "task_takeover"},
            {"run_id": "removed", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest},
            {"run_id": "future", "current_node": "new", "workflow_revision": 3, "workflow_digest": "future"},
        ],
    )

    assert report.compatible is False
    assert report.blocked_count == 4
    assert {item.reason_code for item in report.instances} == {
        "wrong_source_digest",
        "state_profile_incompatible",
        "removed_node_without_mapping",
        "wrong_source_revision",
    }

    profile = simulate_process_migration(
        previous,
        definition(revision=2, steps={"new": {"next": "__end__"}}, state="bug"),
        [{"run_id": "profile-change", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest}],
    )
    assert profile.instances[0].reason_code == "state_profile_incompatible"


def test_simulation_detects_invalid_mapping_targets():
    previous = definition(revision=1, steps={"old": {"next": "__end__"}})
    current = definition(
        revision=2,
        steps={"new": {"next": "__end__"}},
        resume={"old": "missing"},
    )
    report = simulate_process_migration(
        previous,
        current,
        [{"thread_id": "t", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest}],
    )

    assert report.invalid_resume_mappings == ("1:old->missing",)
    assert report.instances[0].reason_code == "invalid_mapping_target"


def test_simulation_detects_same_revision_digest_mutation():
    previous = definition(revision=1, steps={"old": {"next": "__end__"}})
    current = definition(revision=1, steps={"new": {"next": "__end__"}})
    report = simulate_process_migration(
        previous,
        current,
        [{"run_id": "r", "current_node": "old", "workflow_revision": 1, "workflow_digest": previous.digest}],
    )

    assert report.instances[0].reason_code == "same_revision_digest_mutation"
