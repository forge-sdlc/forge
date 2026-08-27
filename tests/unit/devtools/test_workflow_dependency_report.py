"""Tests for the Stage 0 workflow dependency inventory."""

import importlib.util
from pathlib import Path


def _load_report_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "devtools" / "workflow_dependency_report.py"
    spec = importlib.util.spec_from_file_location("workflow_dependency_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_is_deterministic_and_covers_workflow_nodes(tmp_path):
    module = _load_report_module()
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "sample.py").write_text(
        "from forge.integrations.jira.client import JiraClient\n"
        "def run(state):\n"
        "    return state.get('ticket_key'), state['current_node']\n"
    )

    report = module.build_report(nodes)

    assert report["summary"] == {
        "modules": 1,
        "lines": 3,
        "distinct_state_fields": 2,
        "integration_modules": 1,
    }
    assert report["state_fields"] == ["current_node", "ticket_key"]
    assert report["integration_imports"] == ["forge.integrations.jira.client"]


def test_repository_report_parses_every_node_module():
    module = _load_report_module()

    report = module.build_report()

    node_files = list(module.DEFAULT_NODES.glob("*.py"))
    assert report["summary"]["modules"] == len(node_files)
    assert report["summary"]["distinct_state_fields"] > 0
    assert set(report["modules"]) == {path.name for path in node_files}
