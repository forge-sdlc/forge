"""Tests for the container entrypoint git fallback commit."""

import importlib.util
import subprocess
from pathlib import Path


def _load_entrypoint_module():
    module_path = Path(__file__).parents[3] / "containers" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("forge_container_entrypoint", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_commit_excludes_forge_directory(tmp_path):
    entrypoint = _load_entrypoint_module()
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Forge Test")
    _git(tmp_path, "config", "user.email", "forge-test@example.com")

    (tmp_path / "code.txt").write_text("user-facing change\n")
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "handoff.md").write_text("internal handoff\n")

    assert entrypoint.git_commit(tmp_path, "test commit") is True

    tracked = _git(tmp_path, "ls-files").stdout.splitlines()
    assert "code.txt" in tracked
    assert ".forge/handoff.md" not in tracked
