#!/usr/bin/env python3
"""Report workflow-node dependencies on checkpoint fields and integrations."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NODES = ROOT / "src" / "forge" / "workflow" / "nodes"


class DependencyVisitor(ast.NodeVisitor):
    """Collect explicit state reads and Forge integration imports."""

    def __init__(self) -> None:
        self.state_fields: set[str] = set()
        self.integration_imports: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "state"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.state_fields.add(node.args[0].value)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "state"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            self.state_fields.add(node.slice.value)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith("forge.integrations."):
                self.integration_imports.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.startswith("forge.integrations."):
            self.integration_imports.add(node.module)


def build_report(nodes_dir: Path = DEFAULT_NODES) -> dict[str, Any]:
    """Build a deterministic dependency inventory for workflow node modules."""
    modules: dict[str, Any] = {}
    all_fields: set[str] = set()
    provider_modules: set[str] = set()
    total_lines = 0

    for path in sorted(nodes_dir.glob("*.py")):
        source = path.read_text()
        visitor = DependencyVisitor()
        visitor.visit(ast.parse(source, filename=str(path)))
        line_count = len(source.splitlines())
        total_lines += line_count
        all_fields.update(visitor.state_fields)
        provider_modules.update(visitor.integration_imports)
        modules[path.name] = {
            "lines": line_count,
            "state_fields": sorted(visitor.state_fields),
            "integration_imports": sorted(visitor.integration_imports),
        }

    return {
        "summary": {
            "modules": len(modules),
            "lines": total_lines,
            "distinct_state_fields": len(all_fields),
            "integration_modules": len(provider_modules),
        },
        "state_fields": sorted(all_fields),
        "integration_imports": sorted(provider_modules),
        "modules": modules,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes-dir", type=Path, default=DEFAULT_NODES)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    report = build_report(args.nodes_dir)
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
