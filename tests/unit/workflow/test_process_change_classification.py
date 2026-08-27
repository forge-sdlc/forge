"""Focused tests for declarative process-definition change impact."""

from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import (
    ProcessChangeClassification,
    build_process_manifest,
    compare_process_definitions,
    render_mermaid,
)


def definition(
    *,
    revision: int,
    steps: dict,
    state: str = "feature",
    entry: str | None = None,
    mandatory_policies: list[str] | None = None,
):
    return load_workflow_value(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "classification-test", "revision": revision},
            "spec": {
                "state": state,
                "entry": entry or next(iter(steps)),
                "steps": steps,
                **({"mandatoryPolicies": mandatory_policies} if mandatory_policies is not None else {}),
            },
        }
    )


def test_patch_ignores_semantic_definition_order() -> None:
    old = definition(
        revision=1,
        steps={
            "first": {"route": "router", "branches": {"b": "last", "a": "last"}},
            "last": {"next": "__end__"},
        },
        entry="first",
    )
    new = definition(
        revision=2,
        steps={
            "last": {"next": "__end__"},
            "first": {"route": "router", "branches": {"a": "last", "b": "last"}},
        },
        entry="first",
    )

    impact = compare_process_definitions(old, new)

    assert impact.classification is ProcessChangeClassification.PATCH
    assert impact.changed_nodes == ()
    assert impact.compatible_for_in_flight is True


def test_manifest_and_rendering_are_deterministically_ordered() -> None:
    raw = builtin_feature_definition().canonical_dict()
    raw["spec"]["steps"] = dict(reversed(list(raw["spec"]["steps"].items())))
    reordered = load_workflow_value(raw)
    first = build_process_manifest(builtin_feature_definition())
    second = build_process_manifest(reordered)

    assert [node.name for node in second.nodes] == sorted(node.name for node in second.nodes)
    assert [(edge.source, edge.target, edge.outcome or "") for edge in second.transitions] == sorted(
        (edge.source, edge.target, edge.outcome or "") for edge in second.transitions
    )
    assert first.nodes == second.nodes
    assert first.transitions == second.transitions
    assert render_mermaid(first) == render_mermaid(second)


def test_removed_nodes_need_mapping_and_mapped_removal_is_migratable() -> None:
    old = definition(revision=1, steps={"old": {"next": "kept"}, "kept": {"next": "__end__"}})
    unmapped = definition(revision=2, steps={"kept": {"next": "__end__"}}, entry="kept")
    mapped = load_workflow_value(
        {
            **unmapped.canonical_dict(),
            "spec": {
                **unmapped.canonical_dict()["spec"],
                "resume": {"fromRevisions": {"1": {"old": "kept"}}},
            },
        }
    )

    blocked = compare_process_definitions(old, unmapped)
    migrated = compare_process_definitions(old, mapped)

    assert blocked.classification is ProcessChangeClassification.BREAKING
    assert blocked.compatible_for_in_flight is False
    assert migrated.classification is ProcessChangeClassification.MIGRATABLE
    assert migrated.compatible_for_in_flight is True


def test_routing_and_outcome_changes_are_explicit_and_not_silently_compatible() -> None:
    old = definition(
        revision=1,
        steps={
            "route": {"route": "router", "branches": {"ok": "done"}},
            "done": {"next": "__end__"},
            "other": {"next": "__end__"},
        },
    )
    new = definition(
        revision=2,
        steps={
            "route": {"route": "router", "branches": {"ok": "other"}},
            "done": {"next": "__end__"},
            "other": {"next": "__end__"},
        },
    )

    impact = compare_process_definitions(old, new)

    assert impact.routing_changes == ("route",)
    assert impact.outcome_changes == ()
    assert impact.classification is ProcessChangeClassification.MIGRATABLE
    assert impact.compatible_for_in_flight is False


def test_contract_effect_policy_and_execution_changes_are_breaking() -> None:
    old = definition(
        revision=1,
        steps={
            "work": {
                "next": "done",
                "stationContract": "x",
                "stationContractVersion": "1",
                "allowedEffects": ["jira.*"],
                "requiredPolicies": ["p"],
                "retryBound": 2,
            },
            "done": {"next": "__end__"},
        },
    )
    new = definition(
        revision=2,
        steps={
            "work": {
                "next": "done",
                "stationContract": "x",
                "stationContractVersion": "2",
                "allowedEffects": ["source_control.*"],
                "requiredPolicies": ["q"],
                "retryBound": 3,
            },
            "done": {"next": "__end__"},
        },
    )

    impact = compare_process_definitions(old, new)

    assert impact.classification is ProcessChangeClassification.BREAKING
    assert impact.compatible_for_in_flight is False
    assert impact.station_contract_changes == ("work",)
    assert impact.effect_capability_changes == ("work",)
    assert impact.policy_changes == ("work",)
    assert impact.retry_changes == ("work",)


def test_state_profile_and_same_revision_mutation_are_breaking() -> None:
    old = definition(revision=1, steps={"work": {"next": "__end__"}})
    profile = definition(revision=2, steps={"work": {"next": "__end__"}}, state="bug")
    mutated = definition(revision=1, steps={"work": {"next": "__end__"}, "new": {"next": "__end__"}})

    profile_impact = compare_process_definitions(old, profile)
    mutation_impact = compare_process_definitions(old, mutated)

    assert profile_impact.state_profile_changed is True
    assert profile_impact.classification is ProcessChangeClassification.BREAKING
    assert mutation_impact.same_revision_mutation is True
    assert mutation_impact.classification is ProcessChangeClassification.BREAKING
