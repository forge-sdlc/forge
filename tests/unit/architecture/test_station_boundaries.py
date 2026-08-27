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
