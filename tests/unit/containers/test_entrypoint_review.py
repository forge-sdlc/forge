"""Unit tests for review loop integration in entrypoint.py."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add containers to path
sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))


# ---------------------------------------------------------------------------
# Test _create_llm_model
# ---------------------------------------------------------------------------


class TestCreateLlmModel:
    def test_raises_without_credentials(self, monkeypatch):
        """Test that missing credentials raises RuntimeError."""
        monkeypatch.setenv("LLM_BACKEND", "vertex-ai")
        monkeypatch.setenv("LLM_MODEL", "gemini-3.5-flash")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        from entrypoint import _create_llm_model

        with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT is required"):
            _create_llm_model()

    def test_raises_gemini_without_vertex(self, monkeypatch):
        """Test that Gemini model without Vertex AI raises RuntimeError."""
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "gemini-pro")

        from entrypoint import _create_llm_model

        with pytest.raises(RuntimeError, match="not supported by anthropic"):
            _create_llm_model()

    def test_creates_anthropic_model_with_api_key(self, monkeypatch):
        """Test model creation with direct Anthropic API key."""
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5@20250929")

        from entrypoint import _create_llm_model

        name, model = _create_llm_model(max_tokens_default=8192)
        assert name == "claude-sonnet-4-5@20250929"
        assert model is not None

    def test_respects_max_tokens_env_override(self, monkeypatch):
        """Test that LLM_MAX_TOKENS env var overrides default."""
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5@20250929")
        monkeypatch.setenv("LLM_MAX_TOKENS", "4096")

        from entrypoint import _create_llm_model

        name, model = _create_llm_model(max_tokens_default=16384)
        assert model is not None

    def test_creates_vertex_claude_model(self, monkeypatch):
        """Test model creation with Vertex AI for Claude."""
        monkeypatch.setenv("LLM_BACKEND", "vertex-ai")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-east5")
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5@20250929")

        from entrypoint import _create_llm_model

        name, model = _create_llm_model()
        assert name == "claude-sonnet-4-5@20250929"
        assert model is not None


# ---------------------------------------------------------------------------
# Test run_reviewer_agent
# ---------------------------------------------------------------------------


class TestRunReviewerAgent:
    @pytest.mark.asyncio
    async def test_returns_agent_output(self, tmp_path: Path):
        """Test that reviewer agent returns its output text."""
        from entrypoint import run_reviewer_agent

        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="APPROVED")]})
        mock_create = MagicMock(return_value=mock_agent)

        with (
            patch("entrypoint._create_llm_model", return_value=("test-model", MagicMock())),
            patch("deepagents.create_deep_agent", mock_create),
            patch("deepagents.backends.LocalShellBackend"),
        ):
            result = await run_reviewer_agent(
                workspace=tmp_path,
                review_instructions="Check for bugs",
                task_key="TEST-123",
            )

        assert result == "APPROVED"

    @pytest.mark.asyncio
    async def test_system_prompt_contains_instructions(self, tmp_path: Path):
        """Test that review instructions are included in system prompt."""
        from entrypoint import run_reviewer_agent

        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(
            return_value={"messages": [MagicMock(content="REJECTED: bad code")]}
        )
        mock_create = MagicMock(return_value=mock_agent)

        with (
            patch("entrypoint._create_llm_model", return_value=("test-model", MagicMock())),
            patch("deepagents.create_deep_agent", mock_create),
            patch("deepagents.backends.LocalShellBackend"),
        ):
            await run_reviewer_agent(
                workspace=tmp_path,
                review_instructions="Check for security issues",
                task_key="TEST-456",
            )

        call_kwargs = mock_create.call_args[1]
        assert "Check for security issues" in call_kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_uses_8192_max_tokens_default(self, tmp_path: Path):
        """Test that reviewer uses 8192 as default max tokens."""
        from entrypoint import run_reviewer_agent

        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="APPROVED")]})

        with (
            patch(
                "entrypoint._create_llm_model", return_value=("test-model", MagicMock())
            ) as mock_create_model,
            patch("deepagents.create_deep_agent", return_value=mock_agent),
            patch("deepagents.backends.LocalShellBackend"),
        ):
            await run_reviewer_agent(
                workspace=tmp_path,
                review_instructions="Review",
                task_key="TEST-789",
            )

        mock_create_model.assert_called_once_with(max_tokens_default=8192)


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
            cycle_file = (
                tmp_path / ".forge" / "reviews" / "TEST-123__test-skill" / "review_cycle_1.json"
            )
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
            cycle_file = (
                tmp_path
                / ".forge"
                / "reviews"
                / "TEST-123__my-custom-skill"
                / "review_cycle_1.json"
            )
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

            cycle_file = (
                tmp_path / ".forge" / "reviews" / "TEST-123__test-skill" / "review_cycle_1.json"
            )
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
            cycle_file = (
                tmp_path / ".forge" / "reviews" / "TEST-123__test-skill" / "review_cycle_1.json"
            )
            cycle_data = json.loads(cycle_file.read_text())
            assert cycle_data["verdict"] == "rejected"

    @pytest.mark.asyncio
    async def test_worker_retry_failure_writes_exhaustion_cycle(self, tmp_path: Path):
        """Test that worker retry failure writes a synthetic exhaustion cycle file."""
        import sys

        sys.path.insert(0, str(Path(__file__).parents[3] / "containers"))

        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 2\n---\nReview")

        with (
            patch("entrypoint.run_reviewer_agent") as mock_reviewer,
            patch("entrypoint.run_worker_with_feedback") as mock_worker,
        ):
            mock_reviewer.return_value = "REJECTED: bad code"
            mock_worker.return_value = False  # Worker retry fails

            from entrypoint import run_review_loop

            result = await run_review_loop(
                workspace=tmp_path,
                task_key="TEST-456",
                task_summary="Test",
                task_description="Desc",
                guardrails="",
                skill_name="test-skill",
                review_md_path=review_md,
            )

            assert result is True

            # A synthetic exhaustion cycle should have been written
            review_dir = tmp_path / ".forge" / "reviews" / "TEST-456__test-skill"
            cycle_files = list(review_dir.glob("review_cycle_*.json"))
            assert len(cycle_files) >= 2  # original rejection + synthetic exhaustion

            # Find the synthetic one (cycle == max_cycles)
            exhaustion_cycles = [
                json.loads(f.read_text())
                for f in cycle_files
                if json.loads(f.read_text())["cycle"] == json.loads(f.read_text())["max_cycles"]
            ]
            assert len(exhaustion_cycles) >= 1
            assert exhaustion_cycles[0]["verdict"] == "rejected"
            assert "Worker retry failed" in exhaustion_cycles[0]["feedback"]


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

        def _close_and_return_true(coro):
            coro.close()
            return True

        with (
            patch("entrypoint.asyncio.run") as mock_asyncio_run,
            patch("entrypoint.configure_git"),
            patch("entrypoint.subprocess.run") as mock_subprocess,
        ):
            mock_asyncio_run.side_effect = _close_and_return_true
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

        def _close_and_return_true(coro):
            coro.close()
            return True

        with (
            patch("entrypoint.asyncio.run") as mock_asyncio_run,
            patch("entrypoint.configure_git"),
            patch("entrypoint.subprocess.run") as mock_subprocess,
            patch("entrypoint.detect_review_md") as mock_detect,
        ):
            mock_asyncio_run.side_effect = _close_and_return_true
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

        def mock_asyncio_side_effect(coro):
            nonlocal asyncio_call_count
            asyncio_call_count += 1
            coro.close()
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
# Test _discover_skill_paths
# ---------------------------------------------------------------------------


class TestDiscoverSkillPaths:
    """Tests for _discover_skill_paths function."""

    def test_parses_comma_separated_env_var(self, tmp_path: Path, monkeypatch):
        """Test parsing AGENT_SKILL_PATHS env var (comma-separated)."""
        monkeypatch.setenv("AGENT_SKILL_PATHS", "/path/a/,/path/b/,/path/c/")

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        assert "/path/a/" in result
        assert "/path/b/" in result
        assert "/path/c/" in result

    def test_trailing_slash_added_when_missing(self, tmp_path: Path, monkeypatch):
        """Test trailing slash is added when missing."""
        monkeypatch.setenv("AGENT_SKILL_PATHS", "/path/a,/path/b/")

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        assert "/path/a/" in result
        assert "/path/b/" in result

    def test_auto_discovers_claude_skills_dir(self, tmp_path: Path, monkeypatch):
        """Test auto-discovery of .claude/skills workspace dir."""
        monkeypatch.delenv("AGENT_SKILL_PATHS", raising=False)

        # Create .claude/skills directory
        (tmp_path / ".claude" / "skills").mkdir(parents=True)

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        assert f"{tmp_path / '.claude' / 'skills'}/" in result

    def test_auto_discovers_agents_skills_dir(self, tmp_path: Path, monkeypatch):
        """Test auto-discovery of .agents/skills workspace dir."""
        monkeypatch.delenv("AGENT_SKILL_PATHS", raising=False)

        # Create .agents/skills directory
        (tmp_path / ".agents" / "skills").mkdir(parents=True)

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        assert f"{tmp_path / '.agents' / 'skills'}/" in result

    def test_empty_env_returns_only_auto_discovered(self, tmp_path: Path, monkeypatch):
        """Test empty env var returns only auto-discovered paths."""
        monkeypatch.setenv("AGENT_SKILL_PATHS", "")

        # Create one auto-discoverable directory
        (tmp_path / ".claude" / "skills").mkdir(parents=True)

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        # Should only contain auto-discovered path
        assert len(result) == 1
        assert f"{tmp_path / '.claude' / 'skills'}/" in result

    def test_deduplication(self, tmp_path: Path, monkeypatch):
        """Test deduplication between env var and auto-discovered paths."""
        # Create .claude/skills directory
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)

        # Set env var to include the same path that would be auto-discovered
        monkeypatch.setenv("AGENT_SKILL_PATHS", f"{claude_skills}/")

        from entrypoint import _discover_skill_paths

        result = _discover_skill_paths(tmp_path)
        # Should not have duplicates
        assert result.count(f"{claude_skills}/") == 1


# ---------------------------------------------------------------------------
# Test _fallback_commit
# ---------------------------------------------------------------------------


class TestFallbackCommit:
    """Tests for _fallback_commit function."""

    def test_calls_git_commit_when_git_repo(self, tmp_path: Path, monkeypatch):
        """Test it calls git_commit when workspace is a git repo."""
        from unittest.mock import MagicMock

        import entrypoint

        # Mock subprocess.run to return is_git_repo=True
        mock_subprocess_run = MagicMock()
        mock_subprocess_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(entrypoint, "subprocess", MagicMock(run=mock_subprocess_run))

        # Mock git_commit to succeed
        mock_git_commit = MagicMock(return_value=True)
        monkeypatch.setattr(entrypoint, "git_commit", mock_git_commit)

        entrypoint._fallback_commit(tmp_path, "TEST-1", "Test summary")

        mock_git_commit.assert_called_once()
        call_args = mock_git_commit.call_args
        assert call_args[0][0] == tmp_path
        assert "TEST-1" in call_args[0][1]
        assert "Test summary" in call_args[0][1]

    def test_skips_when_not_git_repo(self, tmp_path: Path, monkeypatch):
        """Test it skips when workspace is NOT a git repo."""
        from unittest.mock import MagicMock

        import entrypoint

        # Mock subprocess.run to return is_git_repo=False
        mock_subprocess_run = MagicMock()
        mock_subprocess_run.return_value = MagicMock(returncode=128)
        monkeypatch.setattr(entrypoint, "subprocess", MagicMock(run=mock_subprocess_run))

        mock_git_commit = MagicMock()
        monkeypatch.setattr(entrypoint, "git_commit", mock_git_commit)

        entrypoint._fallback_commit(tmp_path, "TEST-1", "Test summary")

        mock_git_commit.assert_not_called()

    def test_exits_when_git_commit_fails(self, tmp_path: Path, monkeypatch):
        """Test it calls sys.exit(EXIT_TASK_FAILED) when git_commit fails."""
        from unittest.mock import MagicMock

        import entrypoint

        # Mock subprocess.run to return is_git_repo=True
        mock_subprocess_run = MagicMock()
        mock_subprocess_run.return_value = MagicMock(returncode=0)
        monkeypatch.setattr(entrypoint, "subprocess", MagicMock(run=mock_subprocess_run))

        # Mock git_commit to fail
        monkeypatch.setattr(entrypoint, "git_commit", MagicMock(return_value=False))

        with pytest.raises(SystemExit) as exc_info:
            entrypoint._fallback_commit(tmp_path, "TEST-1", "Test summary")

        assert exc_info.value.code == 1  # EXIT_TASK_FAILED


# ---------------------------------------------------------------------------
# Test _setup_langfuse_tracing
# ---------------------------------------------------------------------------


class TestSetupLangfuseTracing:
    """Tests for _setup_langfuse_tracing function."""

    def test_returns_config_with_callbacks_when_key_set(self, monkeypatch):
        """Test returns (config_with_callbacks, True) when LANGFUSE_PUBLIC_KEY is set."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test-123")

        from entrypoint import _setup_langfuse_tracing

        with patch("entrypoint.CallbackHandler", create=True) as mock_handler_cls:
            # Use importlib to make langfuse.langchain.CallbackHandler importable
            mock_handler = MagicMock()
            mock_handler_cls.return_value = mock_handler

            with patch.dict(
                "sys.modules",
                {
                    "langfuse": MagicMock(),
                    "langfuse.langchain": MagicMock(CallbackHandler=mock_handler_cls),
                },
            ):
                config, enabled = _setup_langfuse_tracing("TEST-1", {})

        assert enabled is True
        assert "callbacks" in config
        assert len(config["callbacks"]) == 1

    def test_returns_empty_when_key_not_set(self, monkeypatch):
        """Test returns ({}, False) when LANGFUSE_PUBLIC_KEY is not set."""
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)

        from entrypoint import _setup_langfuse_tracing

        config, enabled = _setup_langfuse_tracing("TEST-1", {})

        assert enabled is False
        assert config == {}

    def test_returns_empty_when_import_fails(self, monkeypatch):
        """Test returns ({}, False) when langfuse import fails."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test-123")

        from entrypoint import _setup_langfuse_tracing

        with patch.dict("sys.modules", {"langfuse": None, "langfuse.langchain": None}):
            config, enabled = _setup_langfuse_tracing("TEST-1", {})

        assert enabled is False
        assert config == {}


# ---------------------------------------------------------------------------
# Test _parse_task_config
# ---------------------------------------------------------------------------


class TestParseTaskConfig:
    """Tests for _parse_task_config function."""

    def test_cli_args_branch(self):
        """Test CLI args branch (--task-summary + --task-description)."""
        from entrypoint import _parse_task_config

        args = MagicMock()
        args.task_file = None
        args.task_summary = "CLI summary"
        args.task_description = "CLI description"

        result = _parse_task_config(args)

        assert result["task_key"] == "UNKNOWN"
        assert result["task_summary"] == "CLI summary"
        assert result["task_description"] == "CLI description"
        assert result["skill_name"] == ""
        assert result["previous_task_keys"] == []
        assert result["trace_context"] == {}

    def test_sys_exit_when_no_args_provided(self):
        """Test sys.exit when neither task-file nor CLI args provided."""
        from entrypoint import _parse_task_config

        args = MagicMock()
        args.task_file = None
        args.task_summary = None
        args.task_description = None

        with pytest.raises(SystemExit) as exc_info:
            _parse_task_config(args)

        assert exc_info.value.code == 3  # EXIT_CONFIG_ERROR

    def test_trace_context_type_guard_non_dict(self, tmp_path: Path):
        """Test trace_context type guard (non-dict becomes {})."""
        from entrypoint import _parse_task_config

        task_file = tmp_path / "task.json"
        task_file.write_text(
            json.dumps(
                {
                    "task_key": "TEST-1",
                    "summary": "Test",
                    "description": "Test desc",
                    "trace_context": "not-a-dict",
                }
            )
        )

        args = MagicMock()
        args.task_file = task_file
        args.task_summary = None
        args.task_description = None

        result = _parse_task_config(args)

        assert result["trace_context"] == {}


# ---------------------------------------------------------------------------
# Test run_reviewer_agent with empty messages
# ---------------------------------------------------------------------------


class TestRunReviewerAgentEmptyMessages:
    """Tests for run_reviewer_agent edge case with empty messages."""

    @pytest.mark.asyncio
    async def test_empty_messages_returns_empty_string(self, tmp_path: Path):
        """Test that empty messages list returns empty string."""
        from entrypoint import run_reviewer_agent

        mock_agent = MagicMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
        mock_create = MagicMock(return_value=mock_agent)

        with (
            patch("entrypoint._create_llm_model", return_value=("test-model", MagicMock())),
            patch("deepagents.create_deep_agent", mock_create),
            patch("deepagents.backends.LocalShellBackend"),
        ):
            result = await run_reviewer_agent(
                workspace=tmp_path,
                review_instructions="Check for bugs",
                task_key="TEST-123",
            )

        assert result == ""


# ---------------------------------------------------------------------------
# Test _print_review_progress (SC-011)
# ---------------------------------------------------------------------------
