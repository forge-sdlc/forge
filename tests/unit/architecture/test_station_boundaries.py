"""Prevent contract-backed stations from reacquiring control-plane coupling."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
STATIONS = ROOT / "src" / "forge" / "workflow" / "stations"
ALLOWED_RUNTIME_IMPORTS = {"forge.effects"}
FORBIDDEN_PREFIXES = (
    "langgraph",
    "forge.orchestrator",
    "forge.integrations.jira",
    "forge.integrations.source_control",
)
NODE_FORBIDDEN_AGENT_PREFIX = "forge.integrations.agents"


def test_station_implementations_do_not_import_graph_queue_or_providers() -> None:
    violations: list[str] = []
    for path in STATIONS.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if path.name == "runner.py" and name in ALLOWED_RUNTIME_IMPORTS:
                    continue
                if name.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.name}:{node.lineno}: {name}")
    assert violations == []


def test_graph_nodes_do_not_execute_agents_or_sandboxes_directly() -> None:
    nodes = ROOT / "src" / "forge" / "workflow" / "nodes"
    violations: list[str] = []
    for path in nodes.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                NODE_FORBIDDEN_AGENT_PREFIX
            ):
                violations.append(f"{path.name}:{node.lineno}: {node.module}")
            if (
                isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "run"
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "runner"
            ):
                violations.append(f"{path.name}:{node.lineno}: direct runner.run")
    assert violations == []


def test_workflow_code_does_not_bypass_the_registered_station_runner() -> None:
    workflow = ROOT / "src" / "forge" / "workflow"
    violations: list[str] = []
    for directory in (workflow / "nodes", workflow / "utils"):
        for path in directory.glob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("forge.workflow.stations."):
                    continue
                for alias in node.names:
                    if alias.name.startswith("run_") and alias.name.endswith("_station"):
                        violations.append(f"{path.name}:{node.lineno}: {alias.name}")
    assert violations == []
