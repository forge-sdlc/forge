"""Tests for forge artifact harvest/restore utilities."""
from pathlib import Path
from typing import Any

import pytest

from forge.workspace.artifacts import harvest_forge_artifacts, restore_forge_artifacts


def _state(**overrides: Any) -> dict:
    return {"forge_artifacts": {}, **overrides}


class TestHarvestForgeArtifacts:
    def test_reads_named_files_into_state(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "handoff.md").write_text("task 1 done")

        state = _state()
        result = harvest_forge_artifacts(tmp_path, "org/repo", ["handoff.md"], state)

        assert result["forge_artifacts"]["org/repo"]["handoff.md"] == "task 1 done"

    def test_skips_files_that_do_not_exist(self, tmp_path):
        (tmp_path / ".forge").mkdir()

        state = _state()
        result = harvest_forge_artifacts(
            tmp_path, "org/repo", ["handoff.md", "fix-plan.md"], state
        )

        assert "handoff.md" not in result["forge_artifacts"].get("org/repo", {})
        assert "fix-plan.md" not in result["forge_artifacts"].get("org/repo", {})

    def test_merges_with_existing_artifacts_for_same_repo(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "fix-plan.md").write_text("fix plan")

        state = _state(forge_artifacts={"org/repo": {"handoff.md": "prior handoff"}})
        result = harvest_forge_artifacts(tmp_path, "org/repo", ["fix-plan.md"], state)

        assert result["forge_artifacts"]["org/repo"]["handoff.md"] == "prior handoff"
        assert result["forge_artifacts"]["org/repo"]["fix-plan.md"] == "fix plan"

    def test_does_not_affect_other_repos(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "handoff.md").write_text("new handoff")

        state = _state(forge_artifacts={"org/other-repo": {"handoff.md": "other handoff"}})
        result = harvest_forge_artifacts(tmp_path, "org/repo", ["handoff.md"], state)

        assert result["forge_artifacts"]["org/other-repo"]["handoff.md"] == "other handoff"
        assert result["forge_artifacts"]["org/repo"]["handoff.md"] == "new handoff"

    def test_overwrites_stale_content_for_same_file(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "handoff.md").write_text("updated handoff")

        state = _state(forge_artifacts={"org/repo": {"handoff.md": "old handoff"}})
        result = harvest_forge_artifacts(tmp_path, "org/repo", ["handoff.md"], state)

        assert result["forge_artifacts"]["org/repo"]["handoff.md"] == "updated handoff"

    def test_returns_new_state_dict_does_not_mutate_input(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "handoff.md").write_text("content")

        state = _state()
        result = harvest_forge_artifacts(tmp_path, "org/repo", ["handoff.md"], state)

        assert result is not state
        assert state["forge_artifacts"] == {}


class TestRestoreForgeArtifacts:
    def test_writes_artifacts_to_forge_dir(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        state = _state(forge_artifacts={"org/repo": {"handoff.md": "restored content"}})

        restore_forge_artifacts(tmp_path, "org/repo", state)

        assert (tmp_path / ".forge" / "handoff.md").read_text() == "restored content"

    def test_creates_parent_dirs_for_nested_files(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        state = _state(forge_artifacts={"org/repo": {"subdir/report.md": "nested content"}})

        restore_forge_artifacts(tmp_path, "org/repo", state)

        assert (tmp_path / ".forge" / "subdir" / "report.md").read_text() == "nested content"

    def test_noop_when_no_artifacts_for_repo(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        state = _state()

        restore_forge_artifacts(tmp_path, "org/repo", state)  # must not raise

        assert list((tmp_path / ".forge").iterdir()) == []

    def test_does_not_restore_artifacts_for_other_repos(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        state = _state(forge_artifacts={"org/other-repo": {"handoff.md": "other content"}})

        restore_forge_artifacts(tmp_path, "org/repo", state)

        assert list((tmp_path / ".forge").iterdir()) == []

    def test_overwrites_existing_file(self, tmp_path):
        (tmp_path / ".forge").mkdir()
        (tmp_path / ".forge" / "handoff.md").write_text("stale content")
        state = _state(forge_artifacts={"org/repo": {"handoff.md": "fresh content"}})

        restore_forge_artifacts(tmp_path, "org/repo", state)

        assert (tmp_path / ".forge" / "handoff.md").read_text() == "fresh content"
