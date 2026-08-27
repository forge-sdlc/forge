"""Prevent retired compatibility paths from returning."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
WORKER = ROOT / "src" / "forge" / "orchestrator" / "worker.py"


def test_worker_exposes_only_generic_ingress_handler() -> None:
    tree = ast.parse(WORKER.read_text(), filename=str(WORKER))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_handle_event" in methods
    assert "_handle_jira_event" not in methods
    assert "_handle_source_control_event" not in methods


def test_worker_does_not_own_observation_to_transition_interpretation() -> None:
    """Observation application must live behind the workflow boundary.

    The worker may normalize, persist, and dispatch an observation.  It must not
    retain the old resume interpreter, which selected nodes from provider event
    kinds and PR/review state.  This guard intentionally checks implementation
    symbols instead of line counts so a compatibility branch cannot quietly be
    reintroduced under a new location in the worker.
    """
    tree = ast.parse(WORKER.read_text(), filename=str(WORKER))
    functions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_handle_resume_event" not in functions
    assert "_deserialize_event" not in functions
    assert "_is_prd_pr_event" not in functions
    assert "_is_spec_pr_event" not in functions

    forbidden_calls = {
        "activate_pull_request_for_event",
        "all_pull_requests_merged",
        "event_targets_pull_request",
        "mark_active_pull_request_merged",
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    calls.update(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    )
    assert not forbidden_calls.intersection(calls)


def test_worker_does_not_branch_on_provider_transition_types() -> None:
    """Provider event/review state is input to the transition policy, not worker code."""
    tree = ast.parse(WORKER.read_text(), filename=str(WORKER))
    forbidden_names = {
        "event_obj",
        "EventKind",
        "ChangeRequestState",
        "ReviewState",
        "targets_implementation_pr",
        "is_ci_webhook",
        "is_approved",
        "is_rejected",
        "is_question",
        "pr_merged",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not forbidden_names.intersection(names)


def test_worker_has_one_explicit_observation_boundary_call() -> None:
    """The process loop must delegate observation application as one operation.

    ``apply_observation`` is the deliberately small port between ingress and
    the pinned workflow definition.  The worker can still record the returned
    state and execute returned feedback/effects, but may not contain another
    event-specific dispatch path.
    """
    tree = ast.parse(WORKER.read_text(), filename=str(WORKER))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_observation_transition"
    ]
    assert len(calls) == 1


def test_removed_compatibility_symbols_cannot_return() -> None:
    source = "\n".join(path.read_text() for path in (ROOT / "src" / "forge").rglob("*.py"))
    for symbol in (
        "LEGACY_SOURCE_CONTROL_STREAM",
        "_LEGACY_SOURCE_VALUES",
        "pin_legacy_state",
        "repository_compatibility_update",
        "legacy_artifacts",
    ):
        assert symbol not in source
    assert not (ROOT / "src" / "forge" / "workflow" / "implementation_input.py").exists()


def test_runtime_registry_uses_only_definition_compiled_golden_paths() -> None:
    registry = (ROOT / "src" / "forge" / "workflow" / "registry.py").read_text()
    assert "FeatureWorkflow" not in registry
    assert "BugWorkflow" not in registry
    assert "TaskTakeoverWorkflow" not in registry
    assert "FeatureGoldenWorkflow" in registry
    assert "BugGoldenWorkflow" in registry
    assert "TaskTakeoverGoldenWorkflow" in registry
