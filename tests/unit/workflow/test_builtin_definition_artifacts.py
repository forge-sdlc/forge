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
    "feature": "f2240bad6450f43ef0cf2787b8890c70e9963d1ae174203e1fb3dbae9d95741e",
    "bug": "4150e2dc6002a1fcb160c59390fb6ad18298d0979d6ee0f5452f0958acad5df8",
    "task_takeover": "3adc871b06bfca01c3f85e46329cc22f5e88d89dde764ce853388e73c2bb9dd8",
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
