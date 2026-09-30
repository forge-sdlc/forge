"""Behavioral checks for mode boundaries and complete review context."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

from entrypoint import repository_snapshot, write_review_diff
from readonly import ReadOnlyFilesystemBackend

from forge.prompts import load_prompt
from forge.sandbox.runner import execution_mode_for_stage
from forge.workflow.nodes.review_utils import collect_git_diff


def git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "code.txt").write_text("base\n")
    git(tmp_path, "add", "code.txt")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "update-ref", "refs/remotes/origin/release/2026", "HEAD")
    return tmp_path


@pytest.mark.parametrize(
    ("step", "skill", "expected"),
    [
        ("analyze_bug", "analyze-bug", "analysis"),
        ("plan_bug_fix", "regenerate-plan", "analysis"),
        ("implement_review_analyze", "implement-review", "analysis"),
        ("implement_review_fix", "implement-review", "implementation"),
        ("task_takeover_review", "task-takeover-review", "review"),
        ("local_review", "local-review-bug", "review"),
        ("local_review", "local-code-review", "implementation"),
        ("rebase", "", "conflict-resolution"),
    ],
)
def test_stage_modes(step, skill, expected):
    assert execution_mode_for_stage(step, skill) == expected


@pytest.mark.asyncio
async def test_review_backend_preserves_files_across_all_write_apis(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("original")
    backend = ReadOnlyFilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)
    assert "original" in backend.read(str(target))
    assert backend.write(str(tmp_path / "new.txt"), "new").error
    assert backend.edit(str(target), "original", "modified").error
    assert (await backend.awrite(str(target), "modified")).error
    assert (await backend.aedit(str(target), "original", "modified")).error
    assert backend.upload_files([(str(target), b"modified")])[0].error
    assert (await backend.aupload_files([(str(target), b"modified")]))[0].error
    assert target.read_text() == "original"
    assert not (tmp_path / "new.txt").exists()
    assert not hasattr(backend, "execute")
    assert not hasattr(backend, "aexecute")


def test_reviews_include_all_commits_and_worktree_on_non_main_base(repo, monkeypatch):
    for index in (1, 2):
        path = repo / f"commit{index}.txt"
        path.write_text(f"change {index}")
        git(repo, "add", path.name)
        git(repo, "commit", "-m", f"change {index}")
    (repo / "code.txt").write_text("uncommitted change\n")
    (repo / ".forge").mkdir()
    (repo / ".forge" / "handoff.md").write_text("internal evidence")
    ops = SimpleNamespace(_run_git=lambda *args, **kwargs: git(repo, *args, **kwargs))
    diff = collect_git_diff(ops, "origin/release/2026")
    monkeypatch.setenv("FORGE_BASE_REF", "origin/release/2026")
    container_diff = write_review_diff(repo).read_text()
    for output in (diff, container_diff):
        assert "commit1.txt" in output and "commit2.txt" in output
        assert "uncommitted change" in output
        assert "internal evidence" not in output


def test_unavailable_base_is_not_an_empty_diff(repo, monkeypatch):
    ops = SimpleNamespace(_run_git=lambda *args, **kwargs: git(repo, *args, **kwargs))
    assert "unavailable" in collect_git_diff(ops, "origin/missing")
    monkeypatch.setenv("FORGE_BASE_REF", "origin/missing")
    assert "unavailable" in write_review_diff(repo).read_text()


def test_analysis_snapshot_ignores_artifacts_but_detects_source_and_commit(repo):
    before = repository_snapshot(repo)
    (repo / ".forge").mkdir()
    (repo / ".forge" / "plan.md").write_text("plan")
    assert repository_snapshot(repo) == before
    (repo / "code.txt").write_text("changed")
    assert repository_snapshot(repo) != before
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "changed")
    assert repository_snapshot(repo) != before


@pytest.mark.parametrize("mode", ["analysis", "review", "conflict-resolution"])
def test_non_implementation_modes_never_run_commit_fallback_or_fix_loop(repo, monkeypatch, mode):
    import entrypoint

    task = repo / "task.json"
    task.write_text(
        json.dumps(
            {
                "task_key": "TEST-1",
                "summary": "Review",
                "description": "Review only",
                "skill_name": "implement-task",
                "execution_mode": mode,
                "stage_instructions": load_prompt(f"container-{mode}"),
            }
        )
    )
    monkeypatch.setattr(
        sys, "argv", ["entrypoint", "--task-file", str(task), "--workspace", str(repo)]
    )
    for name in (
        "FORGE_EXECUTION_MODE",
        "FORGE_STAGE_INSTRUCTIONS",
        "FORGE_SKILL_NAME",
        "FORGE_BASE_REF",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(entrypoint, "configure_git", MagicMock())
    monkeypatch.setattr(entrypoint, "load_guardrails", lambda _: "")

    def complete(coroutine):
        coroutine.close()
        return True

    monkeypatch.setattr(entrypoint.asyncio, "run", complete)
    fallback = MagicMock()
    review = MagicMock()
    monkeypatch.setattr(entrypoint, "_fallback_commit", fallback)
    monkeypatch.setattr(entrypoint, "detect_review_md", review)
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    assert exit_info.value.code == 0
    fallback.assert_not_called()
    review.assert_not_called()
