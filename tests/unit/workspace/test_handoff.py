"""Tests for durable task handoff capture and materialization."""

from forge.workspace.handoff import MAX_HANDOFF_BYTES, capture_handoff, materialize_handoff


def test_capture_records_content_and_metadata(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "handoff.md").write_text("task complete")

    state = {"handoffs": {}}
    result = capture_handoff(tmp_path, "org/repo", "TEST-2", state)

    handoff = result["handoffs"]["org/repo"]
    assert handoff["content"] == "task complete"
    assert handoff["task_key"] == "TEST-2"
    assert handoff["captured_at"]
    assert state["handoffs"] == {}


def test_capture_preserves_other_repositories(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "handoff.md").write_text("new")
    state = {
        "handoffs": {
            "org/other": {"content": "other", "task_key": "X-1", "captured_at": "now"}
        }
    }

    result = capture_handoff(tmp_path, "org/repo", "TEST-2", state)

    assert result["handoffs"]["org/other"] == state["handoffs"]["org/other"]
    assert result["handoffs"]["org/repo"]["content"] == "new"


def test_missing_handoff_removes_stale_saved_value(tmp_path):
    state = {
        "handoffs": {
            "org/repo": {"content": "stale", "task_key": "TEST-1", "captured_at": "now"}
        }
    }

    result = capture_handoff(tmp_path, "org/repo", "TEST-2", state)

    assert "org/repo" not in result["handoffs"]


def test_oversized_handoff_is_not_checkpointed(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "handoff.md").write_bytes(b"x" * (MAX_HANDOFF_BYTES + 1))

    result = capture_handoff(tmp_path, "org/repo", "TEST-2", {"handoffs": {}})

    assert result["handoffs"] == {}


def test_materialize_writes_only_fixed_handoff_path(tmp_path):
    state = {
        "handoffs": {
            "org/repo": {
                "content": "restored",
                "task_key": "../../unsafe",
                "captured_at": "now",
            }
        }
    }

    materialize_handoff(tmp_path, "org/repo", state)

    assert (tmp_path / ".forge" / "handoff.md").read_text() == "restored"
    assert not (tmp_path / ".forge" / "handoff.md.tmp").exists()


def test_materialize_ignores_other_repository(tmp_path):
    state = {
        "handoffs": {
            "org/other": {"content": "other", "task_key": "X-1", "captured_at": "now"}
        }
    }

    materialize_handoff(tmp_path, "org/repo", state)

    assert not (tmp_path / ".forge").exists()


def test_first_iteration_without_saved_handoffs_is_a_noop(tmp_path):
    materialize_handoff(tmp_path, "org/repo", {})

    assert not (tmp_path / ".forge").exists()


def test_malformed_checkpoint_state_is_ignored(tmp_path):
    materialize_handoff(tmp_path, "org/repo", {"handoffs": ["not", "a", "mapping"]})

    assert not (tmp_path / ".forge").exists()


def test_malformed_checkpoint_entry_is_ignored(tmp_path):
    materialize_handoff(tmp_path, "org/repo", {"handoffs": {"org/repo": "bad"}})

    assert not (tmp_path / ".forge").exists()


def test_capture_recovers_from_malformed_checkpoint_state(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "handoff.md").write_text("usable")

    result = capture_handoff(
        tmp_path, "org/repo", "TEST-2", {"handoffs": ["not", "a", "mapping"]}
    )

    assert result["handoffs"]["org/repo"]["content"] == "usable"


def test_capture_rejects_symlinked_handoff(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("do not capture")
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "handoff.md").symlink_to(outside)

    result = capture_handoff(tmp_path, "org/repo", "TEST-2", {"handoffs": {}})

    assert result["handoffs"] == {}


def test_materialize_refuses_symlinked_forge_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".forge").symlink_to(outside, target_is_directory=True)
    state = {
        "handoffs": {
            "org/repo": {"content": "unsafe", "task_key": "TEST-2", "captured_at": "now"}
        }
    }

    materialize_handoff(tmp_path, "org/repo", state)

    assert not (outside / "handoff.md").exists()
