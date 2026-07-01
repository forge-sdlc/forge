"""Unit tests for review loop integration in entrypoint.py."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add containers to path
sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))


# ---------------------------------------------------------------------------
# Test load_conversation_history
# ---------------------------------------------------------------------------


class TestLoadConversationHistory:
    def test_loads_existing_history(self, tmp_path: Path):
        """Test loading conversation history from existing file (SC-003)."""
        from entrypoint import load_conversation_history

        # Create history file
        history_dir = tmp_path / ".forge" / "history"
        history_dir.mkdir(parents=True)
        history_file = history_dir / "TEST-123.json"
        history_data = {
            "task_key": "TEST-123",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        }
        history_file.write_text(json.dumps(history_data))

        result = load_conversation_history(tmp_path, "TEST-123")

        assert result is not None
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        """Test returns None when history file doesn't exist."""
        from entrypoint import load_conversation_history

        result = load_conversation_history(tmp_path, "MISSING-123")

        assert result is None

    def test_returns_none_on_json_error(self, tmp_path: Path):
        """Test returns None when JSON parsing fails."""
        from entrypoint import load_conversation_history

        # Create malformed history file
        history_dir = tmp_path / ".forge" / "history"
        history_dir.mkdir(parents=True)
        history_file = history_dir / "BAD-123.json"
        history_file.write_text("not valid json")

        result = load_conversation_history(tmp_path, "BAD-123")

        assert result is None


# ---------------------------------------------------------------------------
# Test run_worker_with_feedback
# ---------------------------------------------------------------------------


class TestRunWorkerWithFeedback:
    @pytest.mark.asyncio
    async def test_injects_feedback_section(self, tmp_path: Path):
        """Test that feedback is injected into task description (SC-003)."""
        with patch("entrypoint.run_agent_task") as mock_run_agent:
            mock_run_agent.return_value = True

            from entrypoint import run_worker_with_feedback

            result = await run_worker_with_feedback(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Original description",
                guardrails="Some guardrails",
                feedback="Please fix the error handling",
                previous_task_keys=["TEST-122"],
            )

            assert result is True
            mock_run_agent.assert_called_once()

            # Check that feedback was injected
            call_kwargs = mock_run_agent.call_args
            task_description = call_kwargs[1]["task_description"]
            assert "## Reviewer Feedback" in task_description
            assert "Please fix the error handling" in task_description
            assert "Original description" in task_description

    @pytest.mark.asyncio
    async def test_loads_conversation_history(self, tmp_path: Path):
        """Test that conversation history is loaded (SC-003)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create history file
        history_dir = tmp_path / ".forge" / "history"
        history_dir.mkdir(parents=True)
        history_file = history_dir / "TEST-123.json"
        history_data = {"task_key": "TEST-123", "messages": [{"role": "user", "content": "Test"}]}
        history_file.write_text(json.dumps(history_data))

        with patch("entrypoint.run_agent_task") as mock_run_agent:
            mock_run_agent.return_value = True

            from entrypoint import run_worker_with_feedback

            result = await run_worker_with_feedback(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Original description",
                guardrails="",
                feedback="Feedback text",
            )

            assert result is True


# ---------------------------------------------------------------------------
# Test run_review_loop
# ---------------------------------------------------------------------------


class TestRunReviewLoop:
    @pytest.mark.asyncio
    async def test_approved_verdict_exits_successfully(self, tmp_path: Path):
        """Test that APPROVED verdict terminates loop successfully (SC-002)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create review.md
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 3\n---\nReview instructions here")

        with patch("entrypoint.run_reviewer_agent") as mock_reviewer:
            mock_reviewer.return_value = "The code looks great. APPROVED"

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Description",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            assert result is True
            mock_reviewer.assert_called_once()

            # Check cycle file was written
            cycle_file = tmp_path / ".forge" / "test-skill" / "review_cycle_1.json"
            assert cycle_file.exists()
            cycle_data = json.loads(cycle_file.read_text())
            assert cycle_data["verdict"] == "approved"
            assert cycle_data["cycle"] == 1

    @pytest.mark.asyncio
    async def test_rejected_triggers_retry(self, tmp_path: Path):
        """Test that REJECTED verdict triggers retry with feedback (SC-003)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create review.md with 2 retries
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 2\n---\nReview instructions")

        call_count = 0

        async def mock_reviewer_side_effect(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "REJECTED: Missing error handling"
            return "APPROVED"

        with (
            patch("entrypoint.run_reviewer_agent") as mock_reviewer,
            patch("entrypoint.run_worker_with_feedback") as mock_worker,
        ):
            mock_reviewer.side_effect = mock_reviewer_side_effect
            mock_worker.return_value = True

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Description",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            assert result is True
            assert mock_reviewer.call_count == 2
            mock_worker.assert_called_once()

            # Check feedback was passed to worker
            worker_call = mock_worker.call_args
            assert "Missing error handling" in worker_call[1]["feedback"]

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_exits_success(self, tmp_path: Path):
        """Test that max retries exhausted exits with success (BR-005)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create review.md with 2 retries
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 2\n---\nReview instructions")

        with (
            patch("entrypoint.run_reviewer_agent") as mock_reviewer,
            patch("entrypoint.run_worker_with_feedback") as mock_worker,
        ):
            # Always reject
            mock_reviewer.return_value = "REJECTED: Still not good"
            mock_worker.return_value = True

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Description",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            # Should exit with success even after max retries
            assert result is True
            assert mock_reviewer.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_frontmatter_max_retries(self, tmp_path: Path):
        """Test that max_retries from frontmatter is enforced (SC-004)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create review.md with 5 retries
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 5\n---\nReview instructions")

        with (
            patch("entrypoint.run_reviewer_agent") as mock_reviewer,
            patch("entrypoint.run_worker_with_feedback") as mock_worker,
        ):
            mock_reviewer.return_value = "REJECTED"
            mock_worker.return_value = True

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Description",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            # Should have run 5 review cycles
            assert mock_reviewer.call_count == 5
            assert result is True

    @pytest.mark.asyncio
    async def test_uses_env_var_fallback(self, tmp_path: Path, monkeypatch):
        """Test that AUTO_REVIEW_MAX_RETRIES env var is used as fallback (SC-005)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        monkeypatch.setenv("AUTO_REVIEW_MAX_RETRIES", "4")

        # Create review.md without frontmatter
        review_md = tmp_path / "review.md"
        review_md.write_text("Review instructions only, no frontmatter")

        with (
            patch("entrypoint.run_reviewer_agent") as mock_reviewer,
            patch("entrypoint.run_worker_with_feedback") as mock_worker,
        ):
            mock_reviewer.return_value = "REJECTED"
            mock_worker.return_value = True

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Description",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            # Should have run 4 review cycles (from env var)
            assert mock_reviewer.call_count == 4
            assert result is True

    @pytest.mark.asyncio
    async def test_cycle_file_written_to_correct_path(self, tmp_path: Path):
        """Test that cycle file is written to .forge/{step_name}/review_cycle_N.json (SC-007)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 1\n---\nReview")

        with patch("entrypoint.run_reviewer_agent") as mock_reviewer:
            mock_reviewer.return_value = "APPROVED"

            from entrypoint import run_review_loop

            await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test",
                task_description="Desc",
                guardrails="",
                skill_name="my-custom-skill",
                review_md_path=review_md,
            )

            # Check file path matches spec
            cycle_file = tmp_path / ".forge" / "my-custom-skill" / "review_cycle_1.json"
            assert cycle_file.exists()

    @pytest.mark.asyncio
    async def test_reviewer_agent_receives_instructions(self, tmp_path: Path):
        """Test that reviewer agent receives review.md instructions (SC-001)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 1\n---\nCheck for security issues and code quality")

        with patch("entrypoint.run_reviewer_agent") as mock_reviewer:
            mock_reviewer.return_value = "APPROVED"

            from entrypoint import run_review_loop

            await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test",
                task_description="Desc",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            # Check reviewer was called with instructions
            call_kwargs = mock_reviewer.call_args[1]
            assert "Check for security issues" in call_kwargs["review_instructions"]

    @pytest.mark.asyncio
    async def test_cycle_timing_tracked(self, tmp_path: Path):
        """Test that cycle timing is tracked with time.perf_counter()."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 1\n---\nReview")

        with patch("entrypoint.run_reviewer_agent") as mock_reviewer:
            mock_reviewer.return_value = "APPROVED"

            from entrypoint import run_review_loop

            await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test",
                task_description="Desc",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            cycle_file = tmp_path / ".forge" / "test-skill" / "review_cycle_1.json"
            cycle_data = json.loads(cycle_file.read_text())

            # elapsed_seconds should be a positive float
            assert isinstance(cycle_data["elapsed_seconds"], float)
            assert cycle_data["elapsed_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_reviewer_error_treated_as_rejection(self, tmp_path: Path):
        """Test that reviewer agent errors are treated as rejections."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 1\n---\nReview")

        with patch("entrypoint.run_reviewer_agent") as mock_reviewer:
            mock_reviewer.side_effect = RuntimeError("API Error")

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-123",
                task_summary="Test",
                task_description="Desc",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            # Should still exit successfully after max retries
            assert result is True

            # Check that rejection was recorded
            cycle_file = tmp_path / ".forge" / "test-skill" / "review_cycle_1.json"
            cycle_data = json.loads(cycle_file.read_text())
            assert cycle_data["verdict"] == "rejected"


# ---------------------------------------------------------------------------
# Test main function review loop integration
# ---------------------------------------------------------------------------


class TestMainReviewLoopIntegration:
    def test_skips_review_when_no_skill_name(self, tmp_path: Path, monkeypatch):
        """Test that review loop is skipped when no skill_name provided (SC-010)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create task file without skill_name
        task_file = tmp_path / "task.json"
        task_file.write_text(
            json.dumps(
                {
                    "task_key": "TEST-123",
                    "summary": "Test task",
                    "description": "Test description",
                }
            )
        )

        # Create workspace
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("FORGE_SYSTEM_PROMPT_TEMPLATE", "Test prompt {task_key}")
        monkeypatch.chdir(workspace)

        with (
            patch("entrypoint.asyncio.run") as mock_asyncio_run,
            patch("entrypoint.configure_git"),
            patch("entrypoint.subprocess.run") as mock_subprocess,
        ):
            mock_asyncio_run.return_value = True
            mock_subprocess.return_value = MagicMock(returncode=0)

            from entrypoint import main

            with pytest.raises(SystemExit) as exc_info:
                sys.argv = [
                    "entrypoint.py",
                    "--task-file",
                    str(task_file),
                    "--workspace",
                    str(workspace),
                ]
                main()

            # Should exit successfully
            assert exc_info.value.code == 0

    def test_skips_review_when_no_review_md(self, tmp_path: Path, monkeypatch):
        """Test that review loop is skipped when review.md doesn't exist (SC-010)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create task file with skill_name
        task_file = tmp_path / "task.json"
        task_file.write_text(
            json.dumps(
                {
                    "task_key": "TEST-123",
                    "summary": "Test task",
                    "description": "Test description",
                    "skill_name": "nonexistent-skill",
                }
            )
        )

        # Create workspace
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("FORGE_SYSTEM_PROMPT_TEMPLATE", "Test prompt {task_key}")
        monkeypatch.chdir(workspace)

        with (
            patch("entrypoint.asyncio.run") as mock_asyncio_run,
            patch("entrypoint.configure_git"),
            patch("entrypoint.subprocess.run") as mock_subprocess,
            patch("entrypoint.detect_review_md") as mock_detect,
        ):
            mock_asyncio_run.return_value = True
            mock_subprocess.return_value = MagicMock(returncode=0)
            mock_detect.return_value = None  # No review.md found

            from entrypoint import main

            with pytest.raises(SystemExit) as exc_info:
                sys.argv = [
                    "entrypoint.py",
                    "--task-file",
                    str(task_file),
                    "--workspace",
                    str(workspace),
                ]
                main()

            # Should exit successfully without running review loop
            assert exc_info.value.code == 0
            # run_review_loop should not have been called
            # (only run_agent_task via asyncio.run)
            assert mock_asyncio_run.call_count == 1

    def test_runs_review_when_review_md_exists(self, tmp_path: Path, monkeypatch):
        """Test that review loop runs when review.md exists (SC-001)."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        # Create task file with skill_name
        task_file = tmp_path / "task.json"
        task_file.write_text(
            json.dumps(
                {
                    "task_key": "TEST-123",
                    "summary": "Test task",
                    "description": "Test description",
                    "skill_name": "test-skill",
                }
            )
        )

        # Create workspace
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Create review.md
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 1\n---\nReview instructions")

        monkeypatch.setenv("FORGE_SYSTEM_PROMPT_TEMPLATE", "Test prompt {task_key}")
        monkeypatch.chdir(workspace)

        asyncio_call_count = 0

        def mock_asyncio_side_effect(_coro):
            nonlocal asyncio_call_count
            asyncio_call_count += 1
            return True

        with (
            patch("entrypoint.asyncio.run") as mock_asyncio_run,
            patch("entrypoint.configure_git"),
            patch("entrypoint.subprocess.run") as mock_subprocess,
            patch("entrypoint.detect_review_md") as mock_detect,
        ):
            mock_asyncio_run.side_effect = mock_asyncio_side_effect
            mock_subprocess.return_value = MagicMock(returncode=0)
            mock_detect.return_value = review_md

            from entrypoint import main

            with pytest.raises(SystemExit) as exc_info:
                sys.argv = [
                    "entrypoint.py",
                    "--task-file",
                    str(task_file),
                    "--workspace",
                    str(workspace),
                ]
                main()

            assert exc_info.value.code == 0
            # Should have called asyncio.run twice: once for run_agent_task, once for run_review_loop
            assert asyncio_call_count == 2


# ---------------------------------------------------------------------------
# Test _print_review_progress (SC-011)
# ---------------------------------------------------------------------------


class TestPrintReviewProgress:
    """Tests for terminal progress display in local mode (SC-011)."""

    def test_prints_progress_when_tty(self, capsys):
        """Test progress is printed when stdout is a TTY."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        with patch("entrypoint._IS_TTY", True):
            # Need to reload the function to pick up the mocked _IS_TTY
            import importlib

            import entrypoint

            importlib.reload(entrypoint)

            # Re-patch after reload
            with patch.object(entrypoint, "_IS_TTY", True):
                entrypoint._print_review_progress(1, 3, "rejected", "Missing tests")

                captured = capsys.readouterr()
                assert "Review cycle 1/3: REJECTED - Missing tests" in captured.out

    def test_no_output_when_not_tty(self, capsys):
        """Test no output when stdout is not a TTY."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        with patch.object(entrypoint, "_IS_TTY", False):
            entrypoint._print_review_progress(1, 3, "rejected", "Missing tests")

            captured = capsys.readouterr()
            assert captured.out == ""

    def test_feedback_truncated_at_200_chars(self, capsys):
        """Test that feedback is truncated to 200 characters with ellipsis."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        long_feedback = "x" * 300  # 300 characters

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(2, 5, "rejected", long_feedback)

            captured = capsys.readouterr()
            # Should have 200 chars + "..."
            assert "x" * 200 + "..." in captured.out
            # Should NOT have 201 x's
            assert "x" * 201 not in captured.out

    def test_feedback_not_truncated_when_under_200(self, capsys):
        """Test that feedback under 200 chars is not truncated."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        short_feedback = "Fix the error handling in function xyz"

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(1, 3, "rejected", short_feedback)

            captured = capsys.readouterr()
            assert short_feedback in captured.out
            assert "..." not in captured.out

    def test_verdict_uppercased(self, capsys):
        """Test that verdict is uppercased in output."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(1, 3, "approved", "")

            captured = capsys.readouterr()
            assert "APPROVED" in captured.out
            assert "approved" not in captured.out

    def test_no_feedback_shows_just_verdict(self, capsys):
        """Test that empty feedback shows just the verdict without dash."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(3, 3, "approved", "")

            captured = capsys.readouterr()
            assert "Review cycle 3/3: APPROVED" in captured.out
            assert " - " not in captured.out

    def test_format_matches_spec(self, capsys):
        """Test output format matches spec: 'Review cycle N/M: VERDICT - feedback'."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(1, 3, "rejected", "Please add error handling")

            captured = capsys.readouterr()
            expected = "Review cycle 1/3: REJECTED - Please add error handling\n"
            assert captured.out == expected

    def test_feedback_exactly_200_chars_not_truncated(self, capsys):
        """Test that feedback exactly 200 chars is not truncated."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        import importlib

        import entrypoint

        importlib.reload(entrypoint)

        exact_feedback = "y" * 200

        with patch.object(entrypoint, "_IS_TTY", True):
            entrypoint._print_review_progress(1, 2, "rejected", exact_feedback)

            captured = capsys.readouterr()
            assert exact_feedback in captured.out
            assert "..." not in captured.out
