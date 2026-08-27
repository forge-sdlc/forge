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
    "feature": "b06ae46a87c478461d6da5753cf86b20cbd0627c265e42b32ddaebf9441c4ff8",
    "bug": "c756820c98754e6977343e0fe3d1f0065dce17d331b677bbaa0d8952e83d4f9f",
    "task_takeover": "70f204bf2166094174259e70e15e24d8c1ed2a1d6130c0c3b68eb82e4358bd42",
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
