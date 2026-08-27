"""Prevent workflow execution code from bypassing durable mutation ports."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / "src" / "forge" / "workflow"


def test_mutating_workflow_modules_do_not_import_provider_jira_client() -> None:
    violations: list[str] = []
    for path in WORKFLOW.rglob("*.py"):
        relative = path.relative_to(WORKFLOW).as_posix()
        if relative in {"effect_runtime.py", "declarative/cli.py"}:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        has_mutation = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith(
                (
                    "create_",
                    "update_",
                    "delete_",
                    "add_",
                    "remove_",
                    "set_",
                    "transition_",
                    "archive_",
                )
            )
            for node in ast.walk(tree)
        )
        imports_provider_client = any(
            isinstance(node, ast.ImportFrom)
            and node.module in {"forge.integrations.jira", "forge.integrations.jira.client"}
            and any(alias.name == "JiraClient" for alias in node.names)
            for node in ast.walk(tree)
        )
        if has_mutation and imports_provider_client:
            violations.append(relative)
    assert violations == [], f"Workflow mutations bypass the durable Jira port: {violations}"


def test_source_control_resolution_is_centralized_in_durable_port() -> None:
    violations: list[str] = []
    for path in WORKFLOW.rglob("*.py"):
        relative = path.relative_to(WORKFLOW).as_posix()
        if relative in {"effect_runtime.py", "utils/source_control.py"}:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "forge.integrations.source_control.registry"
            ):
                violations.append(relative)
    assert violations == [], f"Workflow code resolves provider adapters directly: {violations}"


def test_repository_pushes_only_execute_through_effect_runtime() -> None:
    violations: list[str] = []
    for path in WORKFLOW.rglob("*.py"):
        relative = path.relative_to(WORKFLOW).as_posix()
        if relative == "effect_runtime.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"push", "push_to_fork"}
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == [], f"Workflow repository pushes bypass durable effects: {violations}"
