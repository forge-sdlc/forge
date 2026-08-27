"""Unit tests to verify task takeover changes for AISOS-2432."""

from pathlib import Path


def test_builtin_task_takeover_file_exists() -> None:
    """Verify that the markdown file exists at the expected path."""
    file_path = Path(__file__).parents[2] / "docs" / "testing" / "builtin-task-20260827-112952.md"
    assert file_path.exists(), f"Expected file not found at {file_path}"


def test_builtin_task_takeover_content() -> None:
    """Verify that the markdown file content is correct."""
    file_path = Path(__file__).parents[2] / "docs" / "testing" / "builtin-task-20260827-112952.md"
    assert file_path.exists(), f"Expected file not found at {file_path}"

    content = file_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    assert len(lines) >= 2, "Markdown file should have at least a heading and a statement."
    assert lines[0].startswith("#"), "Markdown file must start with a heading."
    assert (
        "smoke test completed" in content.lower()
        or "smoke test completed successfully" in content.lower()
    )
