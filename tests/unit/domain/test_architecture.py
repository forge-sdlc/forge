"""Dependency-direction checks for the provider-independent domain package."""

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).parents[3] / "src" / "forge" / "domain"
PROHIBITED_PREFIXES = (
    "langgraph",
    "redis",
    "forge.integrations",
    "forge.orchestrator",
    "forge.queue",
    "forge.workflow",
)


def test_domain_contracts_do_not_import_runtime_or_provider_packages() -> None:
    violations: list[str] = []
    for path in sorted(DOMAIN_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith(PROHIBITED_PREFIXES):
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert not violations, "Prohibited domain dependencies:\n" + "\n".join(violations)
