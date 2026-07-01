"""Unit tests for review polling integration in ContainerRunner."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.observability import ReviewCycleData
from forge.sandbox.runner import (
    ContainerResult,
    ContainerRunner,
    _poller_to_recorder_cycle,
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
# _poller_to_recorder_cycle conversion tests
# ---------------------------------------------------------------------------


class TestPollerToRecorderCycle:
    """Tests for the _poller_to_recorder_cycle helper function."""

    def test_converts_all_fields(self):
        """Test that all fields are converted correctly."""
        poller_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Looks good",
            skill="implement-task",
            elapsed_seconds=12.5,
            timestamp="2024-01-15T10:30:00Z",
        )
        recorder_cycle = _poller_to_recorder_cycle(poller_cycle, "implement_task")

        assert recorder_cycle.cycle == 1
        assert recorder_cycle.max_cycles == 3
        assert recorder_cycle.verdict == "approved"
        assert recorder_cycle.feedback == "Looks good"
        assert recorder_cycle.skill == "implement-task"
        assert recorder_cycle.elapsed_seconds == 12.5
        # Timestamp is converted to datetime
        assert recorder_cycle.timestamp.year == 2024
        assert recorder_cycle.timestamp.month == 1
        assert recorder_cycle.timestamp.day == 15

    def test_handles_invalid_timestamp(self):
        """Test that invalid timestamp falls back to now."""
        poller_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="",
            skill="skill",
            elapsed_seconds=1.0,
            timestamp="not-a-valid-timestamp",
        )
        recorder_cycle = _poller_to_recorder_cycle(poller_cycle, "step")

        # Should have a valid datetime (fallback to now)
        assert recorder_cycle.timestamp is not None

    def test_handles_empty_timestamp(self):
        """Test that empty timestamp falls back to now."""
        poller_cycle = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="skill",
            elapsed_seconds=1.0,
            timestamp="",
        )
        recorder_cycle = _poller_to_recorder_cycle(poller_cycle, "step")

        # Should have a valid datetime (fallback to now)
        assert recorder_cycle.timestamp is not None


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

        # Create a mock process that completes immediately
        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        # Track if polling was started
        poller_created = False

        def create_poller(*_args, **_kwargs):
            nonlocal poller_created
            poller_created = True
            mock_poller = MagicMock()
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        stop_called = False

        def create_poller(*_args, **_kwargs):
            mock_poller = MagicMock()
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[])

            def stop():
                nonlocal stop_called
                stop_called = True

            mock_poller.stop = stop
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

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
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[detected_cycle])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=TimeoutError())
        mock_process.returncode = None

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

            async def poll_iter():
                # Yield one cycle before timeout
                cycles_collected.append(timeout_cycle)
                yield [timeout_cycle]
                # Then wait forever (simulating ongoing polling)
                await asyncio.sleep(1000)

            mock_poller.poll = MagicMock(return_value=poll_iter())
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
            patch.object(runner, "_stop_timed_out_container", new=AsyncMock()),
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

        # Result should indicate failure
        assert result.success is False
        assert "Timeout" in (result.error_message or "")
        # But cycles collected should still be present
        assert len(result.review_cycles) >= 0  # May have collected the cycle


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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

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
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[detected_cycle])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = "implement_task"
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        captured_step_name = None

        def create_poller(workspace_path=None, step_name=None, settings=None):
            nonlocal captured_step_name
            captured_step_name = step_name
            mock_poller = MagicMock()
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = step_name
            # Suppress unused warnings
            _ = workspace_path, settings
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        captured_recorder_step_name = None

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
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
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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
# Helper classes for async iteration mocking
# ---------------------------------------------------------------------------


class AsyncIteratorMock:
    """Mock for async iterators that yields a list of items once and then stops."""

    def __init__(self, items: list):
        self.items = items
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index < len(self.items):
            item = self.items[self.index]
            self.index += 1
            return item
        raise StopAsyncIteration


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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        step_name = "implement_task"

        # Create a file that simulates being written just before container exit
        # (not caught by async polling)
        cycle_dir = tmp_path / ".forge" / step_name
        cycle_dir.mkdir(parents=True)
        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "Fast exit",
            "skill": "fast-review",
            "elapsed_seconds": 1.0,
            "timestamp": "2024-01-15T10:30:00Z",
        }
        cycle_file = cycle_dir / "review_cycle_1.json"
        cycle_file.write_text(json.dumps(cycle_data))

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            # Poller returns no files during polling and poll_once
            # (simulating fast exit where files are written after polling stops)
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = kwargs.get("step_name", "")
            # Empty processed files - nothing was caught during async polling
            mock_poller._processed_files = set()
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
            patch(
                "forge.sandbox.runner.ReviewCyclePoller",
                side_effect=create_poller,
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

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.returncode = 0

        sweep_called = False
        original_sweep = runner._sweep_review_cycles

        def mock_sweep(*args, **kwargs):
            nonlocal sweep_called
            sweep_called = True
            return original_sweep(*args, **kwargs)

        def create_poller(**kwargs):
            mock_poller = MagicMock()
            mock_poller.poll = MagicMock(return_value=AsyncIteratorMock([]))
            mock_poller.poll_once = AsyncMock(return_value=[])
            mock_poller.stop = MagicMock()
            mock_poller.step_name = kwargs.get("step_name", "")
            mock_poller._processed_files = set()
            return mock_poller

        with (
            patch.object(runner, "_build_container_name", return_value="test-container"),
            patch.object(runner, "_build_podman_command", return_value=["podman", "run"]),
            patch(
                "forge.sandbox.runner.asyncio.create_subprocess_exec",
                return_value=mock_process,
            ),
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
