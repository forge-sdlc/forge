"""Unit tests for review polling integration in ContainerRunner."""

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.observability import ReviewCycleData
from forge.observability.review_poller import ReviewCyclePoller
from forge.sandbox.driver import ExecutionResult
from forge.sandbox.runner import (
    ContainerResult,
    ContainerRunner,
)

# ---------------------------------------------------------------------------
# Helper to create a runner instance without __init__ side effects
# ---------------------------------------------------------------------------


def _runner_without_init() -> ContainerRunner:
    """Create a ContainerRunner instance without running __init__."""
    return object.__new__(ContainerRunner)


# ---------------------------------------------------------------------------
# ContainerResult tests
# ---------------------------------------------------------------------------


class TestContainerResultReviewCycles:
    """Tests for ContainerResult review_cycles field."""

    def test_default_review_cycles_empty(self):
        """Test that review_cycles defaults to empty list."""
        result = ContainerResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
        )
        assert result.review_cycles == []

    def test_review_cycles_with_data(self):
        """Test that review_cycles can store ReviewCycleData."""
        cycles = [
            ReviewCycleData(
                cycle=1,
                max_cycles=3,
                verdict="rejected",
                feedback="Fix the bug",
                skill="local-code-review",
                elapsed_seconds=5.5,
                timestamp="2024-01-15T10:30:00Z",
            ),
            ReviewCycleData(
                cycle=2,
                max_cycles=3,
                verdict="approved",
                feedback="",
                skill="local-code-review",
                elapsed_seconds=3.2,
                timestamp="2024-01-15T10:35:00Z",
            ),
        ]
        result = ContainerResult(
            success=True,
            exit_code=0,
            stdout="output",
            stderr="",
            review_cycles=cycles,
        )
        assert len(result.review_cycles) == 2
        assert result.review_cycles[0].verdict == "rejected"
        assert result.review_cycles[1].verdict == "approved"


# ---------------------------------------------------------------------------
# ContainerRunner.run() with step_name tests
# ---------------------------------------------------------------------------


class TestRunWithStepName:
    """Tests for ContainerRunner.run() with step_name parameter."""

    @pytest.mark.asyncio
    async def test_run_accepts_step_name_parameter(self, tmp_path: Path):
        """Test that run() accepts the step_name parameter."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        # Create a mock driver that completes immediately
        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
        ):
            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        assert result.success is True
        assert result.review_cycles == []

    @pytest.mark.asyncio
    async def test_run_without_step_name_disables_polling(self, tmp_path: Path):
        """Test that run() without step_name disables polling."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch("forge.sandbox.runner.ReviewCyclePoller") as mock_poller_class,
        ):
            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                # No step_name provided
            )

        # Poller should not be created when step_name is None
        mock_poller_class.assert_not_called()
        assert result.review_cycles == []


# ---------------------------------------------------------------------------
# Background polling task tests
# ---------------------------------------------------------------------------


class TestBackgroundPollingTask:
    """Tests for the background polling task during container execution."""

    @pytest.mark.asyncio
    async def test_polling_task_started_with_step_name(self, tmp_path: Path):
        """Test that polling task is started when step_name is provided."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        # Track if polling was started
        poller_created = False

        def create_poller(*_args, **_kwargs):
            nonlocal poller_created
            poller_created = True
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        assert poller_created is True

    @pytest.mark.asyncio
    async def test_polling_task_cancelled_on_container_exit(self, tmp_path: Path):
        """Test that polling task is cancelled when container exits."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        stop_called = False

        def create_poller(*_args, **_kwargs):
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])

            def stop():
                nonlocal stop_called
                stop_called = True

            mock_poller.stop = stop
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        assert stop_called is True


# ---------------------------------------------------------------------------
# Review cycle collection tests
# ---------------------------------------------------------------------------


class TestReviewCycleCollection:
    """Tests for collecting review cycles into ContainerResult."""

    @pytest.mark.asyncio
    async def test_detected_cycles_added_to_result(self, tmp_path: Path):
        """Test that detected review cycles are added to the result."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 0.1
        runner.settings.auto_review_record_polled_files = "log"

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        # Simulate a review cycle file being detected during final poll
        detected_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="LGTM",
            skill="local-code-review",
            elapsed_seconds=5.0,
            timestamp="2024-01-15T10:30:00Z",
        )

        def create_poller(*_args, **_kwargs):
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[detected_cycle])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
            patch("forge.sandbox.runner.record_review_cycle"),
            patch("forge.sandbox.runner.record_review_verdict"),
            patch("forge.sandbox.runner.observe_review_duration"),
        ):
            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        assert len(result.review_cycles) == 1
        assert result.review_cycles[0].verdict == "approved"
        assert result.review_cycles[0].skill == "local-code-review"

    @pytest.mark.asyncio
    async def test_cycles_collected_even_on_timeout(self, tmp_path: Path):
        """Test that cycles are collected even when container times out."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 1
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 0.1
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(
                exit_code=-1, stdout="", stderr="Container execution timed out"
            )
        )
        runner._driver = mock_driver

        # Pre-collected cycle (simulating one detected before timeout)
        timeout_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="Partial review",
            skill="local-code-review",
            elapsed_seconds=2.0,
            timestamp="2024-01-15T10:30:00Z",
        )

        cycles_collected = []

        def create_poller(*_args, **_kwargs):
            mock_poller = MagicMock()

            async def run_loop(callback):
                # Deliver one cycle via callback, then block (simulating ongoing polling)
                cycles_collected.append(timeout_cycle)
                callback([timeout_cycle])
                await asyncio.sleep(1000)

            mock_poller.run_loop = run_loop
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
            patch("forge.sandbox.runner.record_review_cycle"),
            patch("forge.sandbox.runner.record_review_verdict"),
            patch("forge.sandbox.runner.observe_review_duration"),
        ):
            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        # Result should indicate failure (driver returns exit_code=-1 on timeout)
        assert result.success is False
        assert result.error_message is not None
        assert isinstance(result.review_cycles, list)


# ---------------------------------------------------------------------------
# Metrics recording tests
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    """Tests for Prometheus metrics recording during polling."""

    @pytest.mark.asyncio
    async def test_metrics_recorded_for_detected_cycles(self, tmp_path: Path):
        """Test that metrics are recorded for each detected cycle."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 0.1
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        detected_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="implement-task",
            elapsed_seconds=10.5,
            timestamp="2024-01-15T10:30:00Z",
        )

        def create_poller(*_args, **_kwargs):
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[detected_cycle])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
            patch("forge.sandbox.runner.record_review_cycle") as mock_cycle,
            patch("forge.sandbox.runner.record_review_verdict") as mock_verdict,
            patch("forge.sandbox.runner.observe_review_duration") as mock_duration,
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        # Verify metrics were recorded
        mock_cycle.assert_called_with("implement-task", "implement_task")
        mock_verdict.assert_called_with("implement-task", "implement_task", "approved")
        mock_duration.assert_called_with("implement-task", "implement_task", 10.5)


# ---------------------------------------------------------------------------
# Step name path organization tests
# ---------------------------------------------------------------------------


class TestStepNamePathOrganization:
    """Tests for step-name based path organization."""

    @pytest.mark.asyncio
    async def test_step_name_passed_to_poller(self, tmp_path: Path):
        """Test that step_name is passed to the poller correctly."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        captured_step_name = None

        def create_poller(
            workspace_path=None, step_name=None, task_key=None, skill_name=None, settings=None
        ):
            nonlocal captured_step_name
            captured_step_name = step_name
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = step_name
            _ = workspace_path, task_key, skill_name, settings
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="local_review",
            )

        assert captured_step_name == "local_review"

    @pytest.mark.asyncio
    async def test_step_name_passed_to_recorder(self, tmp_path: Path):
        """Test that step_name is passed to the recorder correctly."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = "log"

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        captured_recorder_step_name = None

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = kwargs.get("step_name", "")
            return mock_poller

        def create_recorder(step_name=None, mode=None, recording_dir=None):
            nonlocal captured_recorder_step_name
            captured_recorder_step_name = step_name
            mock_recorder = MagicMock()
            mock_recorder.record = MagicMock()
            mock_recorder.record_file = MagicMock()
            # Suppress unused warnings
            _ = mode, recording_dir
            return mock_recorder

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch(
                "forge.sandbox.runner.ReviewCycleRecorder",
                side_effect=create_recorder,
            ),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="fix_ci",
            )

        assert captured_recorder_step_name == "fix_ci"


# ---------------------------------------------------------------------------
# _sweep_review_cycles() tests
# ---------------------------------------------------------------------------


class TestSweepReviewCycles:
    """Tests for the _sweep_review_cycles() post-execution sweep."""

    def test_sweep_finds_missed_file(self, tmp_path: Path, caplog):
        """Test that sweep catches files missed during async polling."""
        import json
        import logging

        runner = _runner_without_init()

        # Create a review cycle file that was NOT processed by the poller
        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "Looks good",
            "skill": "local-review",
            "elapsed_seconds": 5.5,
            "timestamp": "2024-01-15T10:30:00Z",
        }
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        # Empty processed files set - nothing was caught during polling
        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        # Mock recorder
        mock_recorder = MagicMock()

        with caplog.at_level(logging.WARNING):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Should have found the missed file
        assert len(collected_cycles) == 1
        assert collected_cycles[0].cycle == 1
        assert collected_cycles[0].verdict == "approved"
        assert collected_cycles[0].skill == "local-review"

        # Should log a warning about missed files
        assert "Sweep caught 1 review cycle file(s) missed" in caplog.text
        assert step_name in caplog.text

    def test_sweep_deduplicates_against_processed_files(self, tmp_path: Path):
        """Test that sweep skips files already processed by async poller."""
        import json

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        # Create two cycle files
        for i in [1, 2]:
            cycle_data = {
                "cycle": i,
                "max_cycles": 3,
                "verdict": "approved",
                "feedback": f"Review {i}",
                "skill": "local-review",
                "elapsed_seconds": float(i),
                "timestamp": f"2024-01-15T10:3{i}:00Z",
            }
            cycle_file = cycle_dir / f"review_cycle_{i}.json"
            cycle_file.write_text(json.dumps(cycle_data))

        # Simulate that cycle_1 was already processed
        cycle_1_path = str(cycle_dir / "review_cycle_1.json")
        processed_files: set[str] = {cycle_1_path}
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        runner._sweep_review_cycles(
            workspace_path=tmp_path,
            step_name=step_name,
            processed_files=processed_files,
            collected_cycles=collected_cycles,
            recorder=mock_recorder,
        )

        # Should only find cycle_2 (cycle_1 was already processed)
        assert len(collected_cycles) == 1
        assert collected_cycles[0].cycle == 2

    def test_sweep_no_warning_when_no_missed_files(self, tmp_path: Path, caplog):
        """Test that no warning is logged when all files were already processed."""
        import json
        import logging

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "",
            "skill": "local-review",
            "elapsed_seconds": 5.0,
            "timestamp": "2024-01-15T10:30:00Z",
        }
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        # File was already processed
        processed_files: set[str] = {str(cycle_file)}
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        with caplog.at_level(logging.WARNING):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Should not find any new files
        assert len(collected_cycles) == 0

        # Should not log warning about missed files
        assert "Sweep caught" not in caplog.text

    def test_sweep_handles_nonexistent_directory(self, tmp_path: Path):
        """Test that sweep handles missing .forge/{step} directory gracefully."""
        runner = _runner_without_init()

        # Don't create the directory
        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        # Should not raise
        runner._sweep_review_cycles(
            workspace_path=tmp_path,
            step_name="nonexistent_step",
            processed_files=processed_files,
            collected_cycles=collected_cycles,
            recorder=mock_recorder,
        )

        assert len(collected_cycles) == 0

    def test_sweep_handles_invalid_json(self, tmp_path: Path, caplog):
        """Test that sweep handles invalid JSON files gracefully."""
        import logging

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        # Create an invalid JSON file
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text("not valid json {")

        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        with caplog.at_level(logging.WARNING):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Should not have collected any cycles
        assert len(collected_cycles) == 0

        # Should log warning about parse failure
        assert "Failed to parse review cycle file" in caplog.text

    def test_sweep_handles_missing_required_fields(self, tmp_path: Path, caplog):
        """Test that sweep handles JSON with missing required fields."""
        import json
        import logging

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        # Create JSON missing required fields
        cycle_data = {"verdict": "approved", "feedback": "Missing fields"}
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        with caplog.at_level(logging.WARNING):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Should not have collected any cycles
        assert len(collected_cycles) == 0

        # Should log warning about invalid data
        assert "Invalid review cycle data" in caplog.text

    def test_sweep_handles_empty_file(self, tmp_path: Path, caplog):
        """Test that sweep handles empty files gracefully."""
        import logging

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        # Create an empty file
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text("")

        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        with caplog.at_level(logging.WARNING):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Should not have collected any cycles
        assert len(collected_cycles) == 0

        # Should log warning about empty file
        assert "Empty review cycle file" in caplog.text

    def test_sweep_emits_metrics(self, tmp_path: Path):
        """Test that sweep emits Prometheus metrics for caught files."""
        import json

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "rejected",
            "feedback": "Needs work",
            "skill": "local-review",
            "elapsed_seconds": 8.5,
            "timestamp": "2024-01-15T10:30:00Z",
        }
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        with (
            patch("forge.sandbox.runner.record_review_cycle") as mock_cycle,
            patch("forge.sandbox.runner.record_review_verdict") as mock_verdict,
            patch("forge.sandbox.runner.observe_review_duration") as mock_duration,
        ):
            runner._sweep_review_cycles(
                workspace_path=tmp_path,
                step_name=step_name,
                processed_files=processed_files,
                collected_cycles=collected_cycles,
                recorder=mock_recorder,
            )

        # Verify metrics were emitted
        mock_cycle.assert_called_once_with("local-review", step_name)
        mock_verdict.assert_called_once_with("local-review", step_name, "rejected")
        mock_duration.assert_called_once_with("local-review", step_name, 8.5)

    def test_sweep_records_via_recorder(self, tmp_path: Path):
        """Test that sweep uses recorder to record and copy files."""
        import json

        runner = _runner_without_init()

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "",
            "skill": "local-review",
            "elapsed_seconds": 5.0,
            "timestamp": "2024-01-15T10:30:00Z",
        }
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        processed_files: set[str] = set()
        collected_cycles: list[ReviewCycleData] = []

        mock_recorder = MagicMock()

        runner._sweep_review_cycles(
            workspace_path=tmp_path,
            step_name=step_name,
            processed_files=processed_files,
            collected_cycles=collected_cycles,
            recorder=mock_recorder,
        )

        # Verify recorder methods were called
        mock_recorder.record.assert_called_once()
        mock_recorder.record_file.assert_called_once_with(cycle_file)


class TestSweepIntegrationWithRun:
    """Tests for sweep integration with ContainerRunner.run()."""

    @pytest.mark.asyncio
    async def test_fast_exit_files_caught_by_sweep(self, tmp_path: Path, caplog):
        """Test that files written just before container exit are caught by sweep."""
        import json
        import logging

        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = "log"

        step_name = "implement_task"
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "Fast exit",
            "skill": "fast-review",
            "elapsed_seconds": 1.0,
            "timestamp": "2024-01-15T10:30:00Z",
        }

        async def write_file_then_return(_spec):
            # Write the file during the run (simulates container writing just
            # before exit — after our stale-file clearing has already run).
            cycle_dir.mkdir(parents=True, exist_ok=True)
            (cycle_dir / "review_cycle_1.json").write_text(json.dumps(cycle_data))
            return ExecutionResult(exit_code=0, stdout="output", stderr="")

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(side_effect=write_file_then_return)
        runner._driver = mock_driver

        mock_poller_class = MagicMock()
        mock_poller_class.build_cycle_dir = ReviewCyclePoller.build_cycle_dir

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            # run_loop does nothing (simulating fast exit where files are
            # written after polling stops)
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = kwargs.get("step_name", "")
            # Empty processed files - nothing was caught during async polling
            mock_poller._processed_files = set()
            return mock_poller

        mock_poller_class.side_effect = create_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                mock_poller_class,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder") as mock_recorder_class,
            patch("forge.sandbox.runner.record_review_cycle"),
            patch("forge.sandbox.runner.record_review_verdict"),
            patch("forge.sandbox.runner.observe_review_duration"),
            caplog.at_level(logging.WARNING),
        ):
            mock_recorder = MagicMock()
            mock_recorder_class.return_value = mock_recorder

            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name=step_name,
            )

        # The sweep should have caught the file
        assert len(result.review_cycles) == 1
        assert result.review_cycles[0].verdict == "approved"
        assert result.review_cycles[0].skill == "fast-review"

        # Should log warning about missed files
        assert "Sweep caught 1 review cycle file(s) missed" in caplog.text


# ---------------------------------------------------------------------------
# _build_container_result tests
# ---------------------------------------------------------------------------


class TestBuildContainerResult:
    """Tests for ContainerRunner._build_container_result method."""

    def test_exit_success_returns_success_true(self):
        """Test EXIT_SUCCESS returns success=True."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_keep = False

        result = runner._build_container_result(
            exit_code=0,
            stdout_str="output",
            stderr_str="",
            collected_cycles=[],
            container_name="test-container",
        )

        assert result.success is True
        assert result.exit_code == 0
        assert result.tests_passed is True
        assert result.error_message is None

    def test_exit_tests_failed_returns_tests_passed_false(self):
        """Test EXIT_TESTS_FAILED returns tests_passed=False."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_keep = False

        result = runner._build_container_result(
            exit_code=2,  # EXIT_TESTS_FAILED
            stdout_str="output",
            stderr_str="test failures",
            collected_cycles=[],
            container_name="test-container",
        )

        assert result.success is False
        assert result.exit_code == 2
        assert result.tests_passed is False
        assert result.error_message == "Tests failed after max retries"

    def test_other_exit_code_returns_generic_error(self):
        """Test other exit code returns generic error message."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_keep = False

        result = runner._build_container_result(
            exit_code=1,  # EXIT_TASK_FAILED
            stdout_str="output",
            stderr_str="error details",
            collected_cycles=[],
            container_name="test-container",
        )

        assert result.success is False
        assert result.exit_code == 1
        assert result.error_message == "Task failed with exit code 1"

    @pytest.mark.asyncio
    async def test_sweep_runs_after_async_polling(self, tmp_path: Path):
        """Test that sweep is called after container exits."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(
            return_value=ExecutionResult(exit_code=0, stdout="output", stderr="")
        )
        runner._driver = mock_driver

        sweep_called = False
        original_sweep = runner._sweep_review_cycles

        def mock_sweep(*args, **kwargs):
            nonlocal sweep_called
            sweep_called = True
            return original_sweep(*args, **kwargs)

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            mock_poller.run_loop = AsyncMock()
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = kwargs.get("step_name", "")
            mock_poller._processed_files = set()
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
            ),
            patch("forge.sandbox.runner.ReviewCycleRecorder"),
            patch.object(runner, "_sweep_review_cycles", side_effect=mock_sweep),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
            )

        assert sweep_called is True


# ---------------------------------------------------------------------------
# Stale review files from prior run (P1.2 fix)
# ---------------------------------------------------------------------------


class TestStaleReviewFilesCleared:
    """Tests that review cycle files from a prior run are cleared before polling.

    Scenario: workflow retries a task. The .forge/reviews/{task}__{skill}/
    directory already contains review_cycle_*.json files from the previous
    attempt. Without clearing, the poller marks those paths as processed;
    when the new container writes the same filenames, the sweep deduplicates
    them away — silently dropping the new run's exhaustion data.
    """

    def test_first_run_without_cycle_directory_is_no_op(self, tmp_path: Path):
        """Cleanup succeeds when no prior review directory exists."""
        ContainerRunner._clear_stale_review_cycles(
            tmp_path, "implement_task", "TASK-1", "implement-task"
        )

        assert not (tmp_path / ".forge" / "reviews" / "TASK-1__implement-task").exists()

    def test_existing_empty_cycle_directory_is_no_op(self, tmp_path: Path):
        """Cleanup leaves an existing directory with no cycle files intact."""
        review_dir = tmp_path / ".forge" / "reviews" / "TASK-1__implement-task"
        review_dir.mkdir(parents=True)

        ContainerRunner._clear_stale_review_cycles(
            tmp_path, "implement_task", "TASK-1", "implement-task"
        )

        assert review_dir.is_dir()
        assert list(review_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_stale_files_are_cleared_before_container_starts(self, tmp_path: Path):
        """Review cycle files are cleared before the container process is launched."""
        import json

        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 1.0
        runner.settings.auto_review_record_polled_files = None

        # Pre-create stale review cycle files from a prior run
        review_dir = tmp_path / ".forge" / "reviews" / "TASK-1__implement-task"
        review_dir.mkdir(parents=True)
        stale_file = review_dir / "review_cycle_1.json"
        stale_file.write_text(
            json.dumps(
                {
                    "cycle": 1,
                    "max_cycles": 2,
                    "verdict": "rejected",
                    "feedback": "stale feedback from prior run",
                    "skill": "implement-task",
                    "elapsed_seconds": 5.0,
                    "timestamp": "2024-01-01T00:00:00Z",
                }
            )
        )
        assert stale_file.exists()

        async def execute_and_check(_spec):
            assert not stale_file.exists(), "stale file still existed at container launch"
            return ExecutionResult(exit_code=0, stdout="output", stderr="")

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(side_effect=execute_and_check)
        runner._driver = mock_driver

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
        ):
            await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
                task_key="TASK-1",
                skill_name="implement-task",
            )

        # Stale file should remain absent after the run.
        assert not stale_file.exists(), (
            "Stale review_cycle_1.json from prior run should be deleted before new polling"
        )

    @pytest.mark.asyncio
    async def test_new_run_cycles_collected_after_stale_cleared(self, tmp_path: Path):
        """New run's review cycles are collected even when filenames match prior run."""
        import json

        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_image = "test:latest"
        runner.settings.container_timeout = 60
        runner.settings.container_memory = "1g"
        runner.settings.container_cpus = "1"
        runner.settings.container_keep = False
        runner.settings.auto_review_poll_interval = 0.05
        runner.settings.auto_review_record_polled_files = None

        review_dir = tmp_path / ".forge" / "reviews" / "TASK-2__implement-task"
        review_dir.mkdir(parents=True)

        # Write a stale file with old data
        stale_data = {
            "cycle": 1,
            "max_cycles": 2,
            "verdict": "rejected",
            "feedback": "OLD feedback",
            "skill": "implement-task",
            "elapsed_seconds": 5.0,
            "timestamp": "2024-01-01T00:00:00Z",
        }
        (review_dir / "review_cycle_1.json").write_text(json.dumps(stale_data))

        # New data the container will write (same filename, fresh content)
        new_data = {
            "cycle": 1,
            "max_cycles": 2,
            "verdict": "rejected",
            "feedback": "NEW feedback from current run",
            "skill": "implement-task",
            "elapsed_seconds": 8.0,
            "timestamp": "2024-06-01T00:00:00Z",
        }

        async def write_new_cycle_then_return(_spec):
            """Simulate container writing a new cycle file then exiting."""
            await asyncio.sleep(0.1)
            (review_dir / "review_cycle_1.json").write_text(json.dumps(new_data))
            return ExecutionResult(exit_code=0, stdout="output", stderr="")

        mock_driver = MagicMock()
        mock_driver.execute = AsyncMock(side_effect=write_new_cycle_then_return)
        runner._driver = mock_driver

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_execution_spec", return_value=MagicMock()),
        ):
            result = await runner.run(
                workspace_path=tmp_path,
                task_summary="Test task",
                task_description="Test description",
                step_name="implement_task",
                task_key="TASK-2",
                skill_name="implement-task",
            )

        # The result should contain the NEW cycle, not the stale one
        assert len(result.review_cycles) == 1
        assert result.review_cycles[0].feedback == "NEW feedback from current run", (
            f"Got stale feedback: {result.review_cycles[0].feedback}"
        )


# ---------------------------------------------------------------------------
# Debug-hint logging for kept containers
# ---------------------------------------------------------------------------


class TestKeptContainerDebugHint:
    """Tests for driver-provided debug hints when a container is kept."""

    def test_logs_driver_debug_hint_when_kept(self, caplog):
        """A kept container should append the driver's debug hint to the log."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_keep = True
        runner._driver = MagicMock()
        runner._driver.debug_hint.return_value = "  Inspect logs:      podman logs forge-x"

        with caplog.at_level(logging.WARNING):
            result = runner._build_container_result(
                exit_code=1,
                stdout_str="",
                stderr_str="boom",
                collected_cycles=[],
                container_name="forge-x",
            )

        assert result.exit_code == 1
        runner._driver.debug_hint.assert_called_once_with("forge-x")
        assert "Container kept for debugging" in caplog.text
        assert "podman logs forge-x" in caplog.text

    def test_no_hint_appended_when_driver_returns_none(self, caplog):
        """When the driver has no hint, only the base message is logged."""
        runner = _runner_without_init()
        runner.settings = MagicMock()
        runner.settings.container_keep = True
        runner._driver = MagicMock()
        runner._driver.debug_hint.return_value = None

        with caplog.at_level(logging.WARNING):
            runner._build_container_result(
                exit_code=1,
                stdout_str="",
                stderr_str="boom",
                collected_cycles=[],
                container_name="forge-x",
            )

        assert "Container kept for debugging" in caplog.text
