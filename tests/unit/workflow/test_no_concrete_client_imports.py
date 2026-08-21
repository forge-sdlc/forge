import pathlib

WORKFLOW_DIR = pathlib.Path(__file__).resolve().parents[3] / "src" / "forge" / "workflow"


def test_no_workflow_module_imports_github_client():
    offenders = []
    for path in WORKFLOW_DIR.rglob("*.py"):
        text = path.read_text()
        if (
            "from forge.integrations.github.client import" in text
            or "import forge.integrations.github.client" in text
        ):
            offenders.append(str(path.relative_to(WORKFLOW_DIR)))
    assert not offenders, f"workflow modules still import the concrete GitHub client: {offenders}"
