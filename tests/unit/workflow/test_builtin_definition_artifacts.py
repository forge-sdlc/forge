"""Tests for checked-in built-in workflow definition artifacts."""

from __future__ import annotations

import json
from importlib import resources

import pytest

from forge.workflow.declarative.builtins import (
    builtin_bug_definition,
    builtin_feature_definition,
    builtin_task_takeover_definition,
)
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.loader import load_workflow_value

_DEFINITIONS = {
    "feature": builtin_feature_definition,
    "bug": builtin_bug_definition,
    "task_takeover": builtin_task_takeover_definition,
}

# A changed digest is an intentional process revision and must update the
# checked-in artifact and this snapshot together.
_DIGESTS = {
    "feature": "5f653ba647ffbb9cbeda762b363e95eacbc17ed99b82a5d1176ac6fa9c9a8a29",
    "bug": "95d5afd82d727e9eaa1f75f40635536d32ef1b7ef0af84fd9cca2a29683b1d07",
    "task_takeover": "06049d7a4fee8789254b897ece896b004b54a2ae04f2b27a1f173c86129053fb",
}


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_artifact_round_trip_preserves_canonical_definition(name: str) -> None:
    resource = resources.files("forge.workflow.declarative.definitions").joinpath(f"{name}.json")
    artifact = json.loads(resource.read_text(encoding="utf-8"))

    definition = load_workflow_value(artifact)

    assert definition.canonical_dict() == artifact
    expanded = _DEFINITIONS[name]()
    assert not expanded.spec.includes
    assert expanded.spec.resolved_dependencies
    assert set(expanded.spec.steps) > set(definition.spec.steps)


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_builtin_digest_snapshot(name: str) -> None:
    assert _DEFINITIONS[name]().digest == _DIGESTS[name]


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_default_compiler_consumes_checked_in_artifact(name: str) -> None:
    definition = _DEFINITIONS[name]()

    compiler = DeclarativeWorkflowCompiler(definition)
    compiler.validate()
    assert compiler.build_graph() is not None
