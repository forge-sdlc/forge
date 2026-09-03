"""Architecture checks for the normalized ingress boundary."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
WORKER = ROOT / "src" / "forge" / "orchestrator" / "worker.py"
ADAPTERS = ROOT / "src" / "forge" / "orchestrator" / "event_adapters"


def test_worker_never_reads_raw_transport_payload() -> None:
    source = WORKER.read_text()

    assert "message.payload" not in source
    assert "payload.get(" not in source


def test_event_adapters_have_no_runtime_dependencies() -> None:
    prohibited = (
        "redis",
        "langgraph",
        "forge.integrations.jira.client",
        "forge.integrations.source_control.github",
        "forge.queue.consumer",
        "forge.workflow",
    )
    violations: list[str] = []
    for path in ADAPTERS.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith(prohibited):
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert not violations, "Ingress adapter runtime dependencies:\n" + "\n".join(violations)
