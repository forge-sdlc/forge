"""Enforce Phase 8 cutovers and expose any remaining compatibility path."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
INVENTORY = ROOT / "docs" / "architecture" / "phase-8-removal-inventory.json"
WORKER = ROOT / "src" / "forge" / "orchestrator" / "worker.py"


def test_inventory_is_zero_ambiguity_and_remaining_paths_have_evidence() -> None:
    document = json.loads(INVENTORY.read_text())
    assert document["schema_version"] == "2.0"
    entries = document["remaining"] + document["removed"]
    assert len({item["id"] for item in entries}) == len(entries)
    for item in entries:
        assert all(
            item[field].strip()
            for field in ("owner", "prerequisite", "replacement", "proof")
        )
    for item in document["remaining"]:
        assert any((ROOT / path).exists() for path in item["evidence_paths"])


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
