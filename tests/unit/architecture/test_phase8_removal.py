"""Enforce the compatibility-removal checklist and generic ingress boundary."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
INVENTORY = ROOT / "docs" / "architecture" / "phase-8-removal-inventory.json"
WORKER = ROOT / "src" / "forge" / "orchestrator" / "worker.py"


def test_removal_inventory_has_live_evidence_for_every_active_path() -> None:
    document = json.loads(INVENTORY.read_text())

    assert document["schema_version"] == "1.0"
    identifiers = [item["id"] for item in document["paths"]]
    assert len(identifiers) == len(set(identifiers))
    for item in document["paths"]:
        assert item["status"] in {"active", "removed"}
        evidence = [ROOT / path for path in item["evidence_paths"]]
        if item["status"] == "active":
            assert any(path.exists() for path in evidence), item["id"]
        else:
            contents = "\n".join(path.read_text() for path in evidence if path.is_file())
            for symbol in item.get("forbidden_symbols", []):
                assert symbol not in contents, item["id"]
        assert item["replacement"].strip()


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
