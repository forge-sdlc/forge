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
    "feature": "e943904e81d3d3ccdf297be4c0658ad45c41dbeb2247eeb5085ed0bd8bdccd75",
    "bug": "0ebeee8f9a6355fa0dc3f0b6118aa428edcb2c890c2f03162155f67003a4d223",
    "task_takeover": "dd5edd70602ef866dc2fd552b0561456e30ee15ab0e8d41aa5ac7605bfa58cbf",
}


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_artifact_round_trip_preserves_canonical_definition(name: str) -> None:
    resource = resources.files("forge.workflow.declarative.definitions").joinpath(f"{name}.json")
    artifact = json.loads(resource.read_text(encoding="utf-8"))

    definition = load_workflow_value(artifact)

    assert definition.canonical_dict() == artifact
    assert _DEFINITIONS[name]().canonical_dict() == artifact


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_builtin_digest_snapshot(name: str) -> None:
    assert _DEFINITIONS[name]().digest == _DIGESTS[name]


@pytest.mark.parametrize("name", tuple(_DEFINITIONS))
def test_default_compiler_consumes_checked_in_artifact(name: str) -> None:
    definition = _DEFINITIONS[name]()

    compiler = DeclarativeWorkflowCompiler(definition)
    compiler.validate()
    assert compiler.build_graph() is not None
