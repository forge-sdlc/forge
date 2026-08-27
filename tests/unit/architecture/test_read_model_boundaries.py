"""Keep execution inspection a strictly read-only architectural boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
READ_MODELS = ROOT / "src" / "forge" / "read_models"
OPERATOR_ROUTES = ROOT / "src" / "forge" / "api" / "routes"

# These methods either mutate a workflow checkpoint or execute/re-schedule an
# external effect.  Timeline append/purge are intentionally absent: they write
# the projection's own append-only evidence, not workflow/effect state.
MUTATION_METHODS = frozenset(
    {
        "ainvoke",
        "astream",
        "aupdate_state",
        "adelete_thread",
        "advance",
        "claim",
        "claim_due",
        "complete",
        "execute_now",
        "execute_required",
        "replay",
        "retry",
        "submit",
    }
)


def _python_files(directory: Path) -> list[Path]:
    return sorted(set(directory.rglob("*.py")))


def _calls_to_mutation_methods(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [
        f"{path.relative_to(ROOT)}:{node.lineno}:{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in MUTATION_METHODS
    ]


def test_read_models_do_not_mutate_workflows_or_effects() -> None:
    violations = [item for path in _python_files(READ_MODELS) for item in _calls_to_mutation_methods(path)]
    assert violations == [], f"Read-model code crossed a mutation boundary: {violations}"


def test_operator_read_routes_do_not_mutate_workflows_or_effects() -> None:
    # Limit this guard to operator read routes. Webhook/effect routes are
    # mutation surfaces by design and are covered by their own tests.
    operator_files = [OPERATOR_ROUTES / "executions.py", OPERATOR_ROUTES / "org_pulse.py"]
    violations = [item for path in operator_files for item in _calls_to_mutation_methods(path)]
    assert violations == [], f"Operator read API crossed a mutation boundary: {violations}"


def test_read_models_do_not_import_effect_execution_services() -> None:
    violations: list[str] = []
    for path in _python_files(READ_MODELS):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {
                "forge.effects.service",
                "forge.effects.executors",
            }:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}")
    assert violations == [], f"Read-model code imported effect execution services: {violations}"
