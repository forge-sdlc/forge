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
    "feature": "ab7e723fb4938c3dd3a1eff8fd0ea4ba8d067013704cd2c1c61c7f89a79f4384",
    "bug": "08c4869fa5f6e780fd65af8e9b0ccfc2097d1e53990e05ed99d3658b43d3b9d8",
    "task_takeover": "a117afe337faa08ad493abca0137340447420d27396e4cd4e257a1836c90b4f0",
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
