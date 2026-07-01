"""Unit tests for the ReviewCycleRecorder class."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from forge.observability.review_recorder import (
    ReviewCycleData,
    ReviewCycleRecorder,
)


# ---------------------------------------------------------------------------
# ReviewCycleData tests
# ---------------------------------------------------------------------------


class TestReviewCycleData:
    """Tests for ReviewCycleData dataclass."""

    def test_all_fields(self):
        """Test that all fields are correctly stored."""
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Looks good!",
            skill="code-review",
            elapsed_seconds=12.5,
            timestamp=ts,
        )
        assert data.cycle == 1
        assert data.max_cycles == 3
        assert data.verdict == "approved"
        assert data.feedback == "Looks good!"
        assert data.skill == "code-review"
        assert data.elapsed_seconds == 12.5
        assert data.timestamp == ts

    def test_timestamp_is_datetime(self):
        """Test that timestamp field is datetime type."""
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )
        assert isinstance(data.timestamp, datetime)

    def test_to_dict_returns_dict(self):
        """Test to_dict returns a dictionary."""
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=2,
            max_cycles=5,
            verdict="rejected",
            feedback="Needs fixes",
            skill="local-code-review",
            elapsed_seconds=8.3,
            timestamp=ts,
        )
        result = data.to_dict()

        assert isinstance(result, dict)
        assert result["cycle"] == 2
        assert result["max_cycles"] == 5
        assert result["verdict"] == "rejected"
        assert result["feedback"] == "Needs fixes"
        assert result["skill"] == "local-code-review"
        assert result["elapsed_seconds"] == 8.3

    def test_to_dict_timestamp_is_iso_string(self):
        """Test that to_dict converts timestamp to ISO 8601 string."""
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="",
            elapsed_seconds=0.0,
            timestamp=ts,
        )
        result = data.to_dict()

        assert isinstance(result["timestamp"], str)
        assert "2024-01-15" in result["timestamp"]
        assert "10:30:00" in result["timestamp"]

    def test_to_json_returns_string(self):
        """Test that to_json returns a JSON string."""
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Good work",
            skill="review",
            elapsed_seconds=5.0,
            timestamp=ts,
        )
        result = data.to_json()

        assert isinstance(result, str)
        # Should be valid JSON
        parsed = json.loads(result)
        assert parsed["cycle"] == 1
        assert parsed["verdict"] == "approved"

    def test_to_json_pretty_printed(self):
        """Test that to_json output is pretty-printed with indentation."""
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="",
            elapsed_seconds=0.0,
            timestamp=ts,
        )
        result = data.to_json()

        # Should contain newlines (pretty printed)
        assert "\n" in result
        # Should contain indentation
        assert "  " in result


# ---------------------------------------------------------------------------
# ReviewCycleRecorder initialization tests
# ---------------------------------------------------------------------------


class TestReviewCycleRecorderInit:
    """Tests for ReviewCycleRecorder initialization."""

    def test_init_with_step_name_only(self):
        """Test initialization with just step_name."""
        recorder = ReviewCycleRecorder(step_name="implement_task")

        assert recorder.step_name == "implement_task"
        assert recorder.mode is None
        assert recorder.recording_dir is None

    def test_init_with_log_mode(self):
        """Test initialization with log mode."""
        recorder = ReviewCycleRecorder(
            step_name="generate_prd",
            mode="log",
        )

        assert recorder.step_name == "generate_prd"
        assert recorder.mode == "log"

    def test_init_with_copy_mode_and_recording_dir(self):
        """Test initialization with copy mode and recording directory."""
        recorder = ReviewCycleRecorder(
            step_name="implement_task",
            mode="copy",
            recording_dir=Path("/recordings"),
        )

        assert recorder.step_name == "implement_task"
        assert recorder.mode == "copy"
        assert recorder.recording_dir == Path("/recordings")

    def test_init_copy_mode_requires_recording_dir(self):
        """Test that copy mode raises ValueError without recording_dir."""
        with pytest.raises(ValueError, match="recording_dir is required"):
            ReviewCycleRecorder(
                step_name="test_step",
                mode="copy",
            )

    def test_init_log_mode_does_not_require_recording_dir(self):
        """Test that log mode works without recording_dir."""
        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="log",
        )
        assert recorder.recording_dir is None

    def test_step_dir_property(self):
        """Test step_dir property returns correct path."""
        recorder = ReviewCycleRecorder(
            step_name="implement_task",
            mode="copy",
            recording_dir=Path("/recordings"),
        )

        assert recorder.step_dir == Path("/recordings/implement_task")

    def test_step_dir_none_when_no_recording_dir(self):
        """Test step_dir returns None when no recording_dir."""
        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="log",
        )
        assert recorder.step_dir is None


# ---------------------------------------------------------------------------
# ReviewCycleRecorder.record tests (log mode)
# ---------------------------------------------------------------------------


class TestReviewCycleRecorderRecord:
    """Tests for ReviewCycleRecorder.record method."""

    def test_record_disabled_mode_does_nothing(self, caplog):
        """Test that record does nothing when mode is None."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode=None)
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Good",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        # No log messages should be recorded
        assert len(caplog.records) == 0

    def test_record_log_mode_logs_at_info_level(self, caplog):
        """Test that log mode records at INFO level."""
        recorder = ReviewCycleRecorder(step_name="implement_task", mode="log")
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        data = ReviewCycleData(
            cycle=2,
            max_cycles=5,
            verdict="rejected",
            feedback="Fix the tests",
            skill="code-review",
            elapsed_seconds=12.5,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO

    def test_record_log_mode_includes_cycle_info(self, caplog):
        """Test that log output includes cycle number and max cycles."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=3,
            max_cycles=5,
            verdict="approved",
            feedback="",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "3/5" in log_message or ("3" in log_message and "5" in log_message)

    def test_record_log_mode_includes_step_name(self, caplog):
        """Test that log output includes step name."""
        recorder = ReviewCycleRecorder(step_name="implement_task", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "implement_task" in log_message

    def test_record_log_mode_includes_verdict(self, caplog):
        """Test that log output includes verdict."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "rejected" in log_message

    def test_record_log_mode_includes_skill(self, caplog):
        """Test that log output includes skill name."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="local-code-review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "local-code-review" in log_message

    def test_record_log_mode_includes_elapsed_seconds(self, caplog):
        """Test that log output includes elapsed seconds."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="",
            skill="review",
            elapsed_seconds=15.75,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "15.75" in log_message

    def test_record_log_mode_includes_feedback(self, caplog):
        """Test that log output includes feedback."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="Fix the bug in line 42",
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        assert "Fix the bug in line 42" in log_message

    def test_record_log_mode_truncates_long_feedback(self, caplog):
        """Test that log output truncates feedback longer than 100 chars."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")
        ts = datetime.now(timezone.utc)
        long_feedback = "x" * 150
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback=long_feedback,
            skill="review",
            elapsed_seconds=1.0,
            timestamp=ts,
        )

        with caplog.at_level(logging.INFO):
            recorder.record(data)

        log_message = caplog.records[0].message
        # Should truncate and add ellipsis
        assert "..." in log_message
        # Should not contain the full feedback
        assert long_feedback not in log_message


# ---------------------------------------------------------------------------
# ReviewCycleRecorder.record_file tests (copy mode)
# ---------------------------------------------------------------------------


class TestReviewCycleRecorderRecordFile:
    """Tests for ReviewCycleRecorder.record_file method."""

    def test_record_file_disabled_mode_returns_none(self, tmp_path):
        """Test that record_file returns None when mode is None."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode=None)

        # Create a source file
        source_file = tmp_path / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1}')

        result = recorder.record_file(source_file)
        assert result is None

    def test_record_file_log_mode_returns_none(self, tmp_path):
        """Test that record_file returns None when mode is log."""
        recorder = ReviewCycleRecorder(step_name="test_step", mode="log")

        # Create a source file
        source_file = tmp_path / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1}')

        result = recorder.record_file(source_file)
        assert result is None

    def test_record_file_copy_mode_creates_step_directory(self, tmp_path):
        """Test that copy mode creates the step-specific subdirectory."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="implement_task",
            mode="copy",
            recording_dir=recording_dir,
        )

        # Create a source file
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        source_file = source_dir / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1}')

        recorder.record_file(source_file)

        step_dir = recording_dir / "implement_task"
        assert step_dir.exists()
        assert step_dir.is_dir()

    def test_record_file_copy_mode_copies_file(self, tmp_path):
        """Test that copy mode copies the file to recording directory."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="implement_task",
            mode="copy",
            recording_dir=recording_dir,
        )

        # Create a source file with content
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        source_file = source_dir / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1, "verdict": "approved"}')

        result = recorder.record_file(source_file)

        expected_dest = recording_dir / "implement_task" / "review_cycle_1.json"
        assert result == expected_dest
        assert expected_dest.exists()
        assert expected_dest.read_text() == '{"cycle": 1, "verdict": "approved"}'

    def test_record_file_copy_mode_preserves_filename(self, tmp_path):
        """Test that copy mode preserves the original filename."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="copy",
            recording_dir=recording_dir,
        )

        source_dir = tmp_path / "source"
        source_dir.mkdir()
        source_file = source_dir / "review_cycle_5.json"
        source_file.write_text('{"cycle": 5}')

        result = recorder.record_file(source_file)

        assert result is not None
        assert result.name == "review_cycle_5.json"

    def test_record_file_copy_mode_handles_multiple_files(self, tmp_path):
        """Test that multiple files can be copied."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="copy",
            recording_dir=recording_dir,
        )

        source_dir = tmp_path / "source"
        source_dir.mkdir()

        # Copy multiple files
        for i in range(1, 4):
            source_file = source_dir / f"review_cycle_{i}.json"
            source_file.write_text(f'{{"cycle": {i}}}')
            recorder.record_file(source_file)

        step_dir = recording_dir / "test_step"
        assert (step_dir / "review_cycle_1.json").exists()
        assert (step_dir / "review_cycle_2.json").exists()
        assert (step_dir / "review_cycle_3.json").exists()

    def test_record_file_nonexistent_source_returns_none(self, tmp_path, caplog):
        """Test that nonexistent source file returns None and logs warning."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="copy",
            recording_dir=recording_dir,
        )

        nonexistent_file = tmp_path / "missing.json"

        with caplog.at_level(logging.WARNING):
            result = recorder.record_file(nonexistent_file)

        assert result is None
        assert any("does not exist" in record.message for record in caplog.records)

    def test_record_file_returns_path_on_success(self, tmp_path):
        """Test that successful copy returns the destination path."""
        recording_dir = tmp_path / "recordings"
        recorder = ReviewCycleRecorder(
            step_name="my_step",
            mode="copy",
            recording_dir=recording_dir,
        )

        source_dir = tmp_path / "source"
        source_dir.mkdir()
        source_file = source_dir / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1}')

        result = recorder.record_file(source_file)

        assert result is not None
        assert result == recording_dir / "my_step" / "review_cycle_1.json"

    def test_record_file_directory_creation_error_returns_none(self, tmp_path, caplog):
        """Test that directory creation error returns None."""
        # Create a file where the directory should be
        recording_dir = tmp_path / "recordings"
        recording_dir.mkdir()
        blocking_file = recording_dir / "test_step"
        blocking_file.write_text("blocking file")

        recorder = ReviewCycleRecorder(
            step_name="test_step",
            mode="copy",
            recording_dir=recording_dir,
        )

        source_dir = tmp_path / "source"
        source_dir.mkdir()
        source_file = source_dir / "review_cycle_1.json"
        source_file.write_text('{"cycle": 1}')

        with caplog.at_level(logging.ERROR):
            result = recorder.record_file(source_file)

        assert result is None
        assert any("Failed to create directory" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Settings configuration tests
# ---------------------------------------------------------------------------


class TestSettingsConfiguration:
    """Tests for AUTO_REVIEW_RECORD_POLLED_FILES configuration."""

    def test_setting_default_is_none(self):
        """Test that the default value is None."""
        from forge.config import Settings

        with patch.dict("os.environ", {}, clear=True):
            # Create settings with minimal required fields
            settings = Settings(
                jira_base_url="https://example.atlassian.net",
                jira_api_token="token",
                jira_user_email="user@example.com",
                github_token="ghtoken",
            )
            assert settings.auto_review_record_polled_files is None

    def test_setting_accepts_log_value(self):
        """Test that 'log' value is accepted."""
        from forge.config import Settings

        with patch.dict("os.environ", {"AUTO_REVIEW_RECORD_POLLED_FILES": "log"}, clear=False):
            settings = Settings(
                jira_base_url="https://example.atlassian.net",
                jira_api_token="token",
                jira_user_email="user@example.com",
                github_token="ghtoken",
            )
            assert settings.auto_review_record_polled_files == "log"

    def test_setting_accepts_copy_value(self):
        """Test that 'copy' value is accepted."""
        from forge.config import Settings

        with patch.dict("os.environ", {"AUTO_REVIEW_RECORD_POLLED_FILES": "copy"}, clear=False):
            settings = Settings(
                jira_base_url="https://example.atlassian.net",
                jira_api_token="token",
                jira_user_email="user@example.com",
                github_token="ghtoken",
            )
            assert settings.auto_review_record_polled_files == "copy"
