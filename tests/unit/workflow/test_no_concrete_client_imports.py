import pathlib
import re

SRC_DIR = pathlib.Path(__file__).resolve().parents[3] / "src" / "forge"
WORKFLOW_DIR = SRC_DIR / "workflow"
WORKSPACE_DIR = SRC_DIR / "workspace"

# Matches a legacy or provider-specific source-control implementation,
# e.g. `from forge.integrations.github.client import GitHubClient` or an import
# from the concrete GitHub adapter package. This is a textual
# scan, not an import graph: it does NOT catch a two-step alias like
# `from forge.integrations import github` followed later by
# `github.client.GitHubClient(...)`, since that indirection never spells out
# `integrations.github` as one dotted token. It also only scans
# WORKFLOW_DIR/WORKSPACE_DIR below, not other modules (e.g. worker.py) that
# were migrated onto get_adapter.
_CONCRETE_CLIENT_PATTERN = re.compile(
    r"integrations\.github(?:\.client)?\b|integrations\.source_control\.github\b"
)


def _find_offenders(directory: pathlib.Path) -> list[str]:
    offenders = []
    for path in directory.rglob("*.py"):
        text = path.read_text()
        if _CONCRETE_CLIENT_PATTERN.search(text):
            offenders.append(str(path.relative_to(directory)))
    return offenders


def test_no_workflow_module_imports_concrete_source_control_provider():
    offenders = _find_offenders(WORKFLOW_DIR)
    assert not offenders, f"workflow modules still import the concrete GitHub client: {offenders}"


def test_no_workspace_module_imports_concrete_source_control_provider():
    offenders = _find_offenders(WORKSPACE_DIR)
    assert not offenders, f"workspace modules still import the concrete GitHub client: {offenders}"
