import pathlib
import re

SRC_DIR = pathlib.Path(__file__).resolve().parents[3] / "src" / "forge"
WORKFLOW_DIR = SRC_DIR / "workflow"
WORKSPACE_DIR = SRC_DIR / "workspace"

# Matches any dotted reference to the concrete client module or its parent
# package, e.g. `from forge.integrations.github.client import GitHubClient`
# or `forge.integrations.github.client.GitHubClient(...)`. This is a textual
# scan, not an import graph: it does NOT catch a two-step alias like
# `from forge.integrations import github` followed later by
# `github.client.GitHubClient(...)`, since that indirection never spells out
# `integrations.github` as one dotted token. It also only scans
# WORKFLOW_DIR/WORKSPACE_DIR below, not other modules (e.g. worker.py) that
# were migrated onto get_adapter.
_CONCRETE_CLIENT_PATTERN = re.compile(r"integrations\.github\.client|integrations\.github\b")


def _find_offenders(directory: pathlib.Path) -> list[str]:
    offenders = []
    for path in directory.rglob("*.py"):
        text = path.read_text()
        if _CONCRETE_CLIENT_PATTERN.search(text):
            offenders.append(str(path.relative_to(directory)))
    return offenders


def test_no_workflow_module_imports_github_client():
    offenders = _find_offenders(WORKFLOW_DIR)
    assert not offenders, f"workflow modules still import the concrete GitHub client: {offenders}"


def test_no_workspace_module_imports_github_client():
    offenders = _find_offenders(WORKSPACE_DIR)
    assert not offenders, f"workspace modules still import the concrete GitHub client: {offenders}"
