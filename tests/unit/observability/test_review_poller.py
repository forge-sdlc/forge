"""Unit tests for the ReviewCyclePoller class."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from forge.config import Settings
from forge.observability.review_poller import (
    MAX_JSON_PARSE_RETRIES,
    ReviewCycleData,
    ReviewCyclePoller,
)

# ---------------------------------------------------------------------------
# ReviewCycleData tests
# ---------------------------------------------------------------------------


class TestReviewCycleData:
    """Tests for ReviewCycleData dataclass."""

    def test_all_fields(self):
        """Test that all fields are correctly stored."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Looks good!",
            skill="code-review",
            elapsed_seconds=12.5,
            timestamp="2024-01-15T10:30:00Z",
            file_path="/path/to/file.json",
        )
        assert data.cycle == 1
        assert data.max_cycles == 3
        assert data.verdict == "approved"
        assert data.feedback == "Looks good!"
        assert data.skill == "code-review"
        assert data.elapsed_seconds == 12.5
        assert data.timestamp == "2024-01-15T10:30:00Z"
        assert data.file_path == "/path/to/file.json"

    def test_default_file_path(self):
        """Test that file_path defaults to empty string."""
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="rejected",
            feedback="Needs work",
            skill="review",
            elapsed_seconds=5.0,
            timestamp="2024-01-15T10:30:00Z",
        )
        assert data.file_path == ""

    def test_from_dict_all_fields(self):
        """Test creating from dict with all fields."""
        input_dict = {
            "cycle": 2,
            "max_cycles": 5,
            "verdict": "rejected",
            "feedback": "Fix bugs",
            "skill": "local-code-review",
            "elapsed_seconds": 8.3,
            "timestamp": "2024-01-16T14:20:00Z",
        }
        data = ReviewCycleData.from_dict(input_dict, file_path="/test/path.json")

        assert data.cycle == 2
        assert data.max_cycles == 5
        assert data.verdict == "rejected"
        assert data.feedback == "Fix bugs"
        assert data.skill == "local-code-review"
        assert data.elapsed_seconds == 8.3
        assert data.timestamp == "2024-01-16T14:20:00Z"
        assert data.file_path == "/test/path.json"

    def test_from_dict_minimal_fields(self):
        """Test creating from dict with only required fields."""
        input_dict = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
        }
        data = ReviewCycleData.from_dict(input_dict)

        assert data.cycle == 1
        assert data.max_cycles == 3
        assert data.verdict == "approved"
        assert data.feedback == ""
        assert data.skill == ""
        assert data.elapsed_seconds == 0.0
        assert data.timestamp == ""
        assert data.file_path == ""

    def test_from_dict_missing_required_raises(self):
        """Test that missing required fields raise KeyError."""
        with pytest.raises(KeyError):
            ReviewCycleData.from_dict({"cycle": 1, "max_cycles": 3})  # missing verdict

        with pytest.raises(KeyError):
            ReviewCycleData.from_dict({"cycle": 1, "verdict": "approved"})  # missing max_cycles


# ---------------------------------------------------------------------------
# ReviewCyclePoller initialization tests
# ---------------------------------------------------------------------------


class TestReviewCyclePollerInit:
    """Tests for ReviewCyclePoller initialization."""

    def test_init_basic(self):
        """Test basic initialization."""
        poller = ReviewCyclePoller(
            workspace_path=Path("/workspace"),
            step_name="implement_task",
        )
        assert poller.workspace_path == Path("/workspace")
        assert poller.step_name == "implement_task"
        assert poller._settings is None
        assert poller._processed_files == set()
        assert poller._running is False

    def test_init_with_settings(self, mock_settings):
        """Test initialization with explicit settings."""
        poller = ReviewCyclePoller(
            workspace_path=Path("/workspace"),
            step_name="generate_prd",
            settings=mock_settings,
        )
        assert poller._settings is mock_settings

    def test_review_cycle_dir(self):
        """Test review_cycle_dir property."""
        poller = ReviewCyclePoller(
            workspace_path=Path("/workspace"),
            step_name="implement_task",
        )
        assert poller.review_cycle_dir == Path("/workspace/.forge/implement_task")

    def test_poll_interval_from_settings(self, mock_settings):
        """Test poll_interval reads from settings."""
        mock_settings.auto_review_poll_interval = 10.0
        poller = ReviewCyclePoller(
            workspace_path=Path("/workspace"),
            step_name="test_step",
            settings=mock_settings,
        )
        assert poller.poll_interval == 10.0


# ---------------------------------------------------------------------------
# ReviewCyclePoller._get_review_cycle_files tests
# ---------------------------------------------------------------------------


class TestGetReviewCycleFiles:
    """Tests for _get_review_cycle_files method."""

    def test_no_directory_returns_empty(self, tmp_path):
        """Test that non-existent directory returns empty list."""
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="nonexistent_step",
        )
        assert poller._get_review_cycle_files() == []

    def test_empty_directory_returns_empty(self, tmp_path):
        """Test that empty directory returns empty list."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        assert poller._get_review_cycle_files() == []

    def test_finds_review_cycle_files(self, tmp_path):
        """Test finding review_cycle_*.json files."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        # Create matching files
        (step_dir / "review_cycle_1.json").write_text("{}")
        (step_dir / "review_cycle_2.json").write_text("{}")
        (step_dir / "review_cycle_10.json").write_text("{}")

        # Create non-matching files
        (step_dir / "other_file.json").write_text("{}")
        (step_dir / "review_cycle.json").write_text("{}")  # Missing underscore
        (step_dir / "review_cycle_1.txt").write_text("{}")  # Wrong extension

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        files = poller._get_review_cycle_files()

        assert len(files) == 3
        assert all(f.name.startswith("review_cycle_") for f in files)
        assert all(f.suffix == ".json" for f in files)

    def test_files_are_sorted(self, tmp_path):
        """Test that files are returned in sorted order."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        # Create files in non-sorted order
        (step_dir / "review_cycle_3.json").write_text("{}")
        (step_dir / "review_cycle_1.json").write_text("{}")
        (step_dir / "review_cycle_2.json").write_text("{}")

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        files = poller._get_review_cycle_files()

        assert [f.name for f in files] == [
            "review_cycle_1.json",
            "review_cycle_2.json",
            "review_cycle_3.json",
        ]


# ---------------------------------------------------------------------------
# ReviewCyclePoller._parse_json_with_retry tests
# ---------------------------------------------------------------------------


class TestParseJsonWithRetry:
    """Tests for _parse_json_with_retry method."""

    @pytest.mark.asyncio
    async def test_parse_valid_json(self, tmp_path):
        """Test parsing a valid JSON file."""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"cycle": 1, "max_cycles": 3, "verdict": "approved"}')

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
        )
        result = await poller._parse_json_with_retry(json_file)

        assert result == {"cycle": 1, "max_cycles": 3, "verdict": "approved"}

    @pytest.mark.asyncio
    async def test_parse_empty_file_returns_none(self, tmp_path):
        """Test that empty file returns None after retries."""
        json_file = tmp_path / "empty.json"
        json_file.write_text("")

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await poller._parse_json_with_retry(json_file)

        assert result is None
        # Should have retried MAX_JSON_PARSE_RETRIES - 1 times
        assert mock_sleep.await_count == MAX_JSON_PARSE_RETRIES - 1

    @pytest.mark.asyncio
    async def test_parse_invalid_json_retries(self, tmp_path):
        """Test that invalid JSON triggers retries."""
        json_file = tmp_path / "invalid.json"
        json_file.write_text('{"incomplete": ')

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await poller._parse_json_with_retry(json_file)

        assert result is None
        assert mock_sleep.await_count == MAX_JSON_PARSE_RETRIES - 1

    @pytest.mark.asyncio
    async def test_parse_file_not_found_returns_none(self, tmp_path):
        """Test that missing file returns None without retries."""
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
        )
        result = await poller._parse_json_with_retry(tmp_path / "nonexistent.json")

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_succeeds_on_retry(self, tmp_path):
        """Test that parsing can succeed after initial failures."""
        json_file = tmp_path / "eventually_valid.json"
        json_file.write_text('{"incomplete": ')

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
        )

        call_count = 0

        async def mock_read():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return '{"incomplete": '
            return '{"cycle": 1, "max_cycles": 3, "verdict": "approved"}'

        with patch("aiofiles.open") as mock_open:
            mock_file = AsyncMock()
            mock_file.read = mock_read
            mock_file.__aenter__ = AsyncMock(return_value=mock_file)
            mock_file.__aexit__ = AsyncMock(return_value=None)
            mock_open.return_value = mock_file

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await poller._parse_json_with_retry(json_file)

        assert result == {"cycle": 1, "max_cycles": 3, "verdict": "approved"}


# ---------------------------------------------------------------------------
# ReviewCyclePoller.poll_once tests
# ---------------------------------------------------------------------------


class TestPollOnce:
    """Tests for poll_once method."""

    @pytest.mark.asyncio
    async def test_poll_once_no_files(self, tmp_path):
        """Test poll_once with no files."""
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        result = await poller.poll_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_poll_once_new_files(self, tmp_path):
        """Test poll_once detects new files."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "LGTM",
            "skill": "code-review",
            "elapsed_seconds": 5.5,
            "timestamp": "2024-01-15T10:00:00Z",
        }
        (step_dir / "review_cycle_1.json").write_text(json.dumps(cycle_data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        result = await poller.poll_once()

        assert len(result) == 1
        assert result[0].cycle == 1
        assert result[0].verdict == "approved"
        assert result[0].feedback == "LGTM"
        assert result[0].skill == "code-review"

    @pytest.mark.asyncio
    async def test_poll_once_skips_already_processed(self, tmp_path):
        """Test that poll_once skips already processed files."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "approved",
            "feedback": "",
            "skill": "",
            "elapsed_seconds": 0,
            "timestamp": "",
        }
        file_path = step_dir / "review_cycle_1.json"
        file_path.write_text(json.dumps(cycle_data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )

        # First poll should detect the file
        result1 = await poller.poll_once()
        assert len(result1) == 1

        # Second poll should skip it
        result2 = await poller.poll_once()
        assert len(result2) == 0

    @pytest.mark.asyncio
    async def test_poll_once_detects_new_after_first(self, tmp_path):
        """Test that poll_once detects new files on subsequent polls."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        cycle1 = {"cycle": 1, "max_cycles": 3, "verdict": "rejected", "feedback": "Fix it"}
        (step_dir / "review_cycle_1.json").write_text(json.dumps(cycle1))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )

        result1 = await poller.poll_once()
        assert len(result1) == 1
        assert result1[0].verdict == "rejected"

        # Add new file
        cycle2 = {"cycle": 2, "max_cycles": 3, "verdict": "approved", "feedback": ""}
        (step_dir / "review_cycle_2.json").write_text(json.dumps(cycle2))

        result2 = await poller.poll_once()
        assert len(result2) == 1
        assert result2[0].cycle == 2
        assert result2[0].verdict == "approved"

    @pytest.mark.asyncio
    async def test_poll_once_skips_invalid_json(self, tmp_path):
        """Test that invalid JSON files are skipped."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        # Valid file
        valid_data = {"cycle": 1, "max_cycles": 3, "verdict": "approved"}
        (step_dir / "review_cycle_1.json").write_text(json.dumps(valid_data))

        # Invalid JSON file
        (step_dir / "review_cycle_2.json").write_text("not valid json")

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poller.poll_once()

        # Only valid file should be returned
        assert len(result) == 1
        assert result[0].cycle == 1

    @pytest.mark.asyncio
    async def test_poll_once_skips_missing_required_fields(self, tmp_path):
        """Test that files with missing required fields are skipped."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        # Missing 'verdict'
        invalid_data = {"cycle": 1, "max_cycles": 3}
        (step_dir / "review_cycle_1.json").write_text(json.dumps(invalid_data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )
        result = await poller.poll_once()

        assert len(result) == 0


# ---------------------------------------------------------------------------
# ReviewCyclePoller async iteration tests
# ---------------------------------------------------------------------------


class TestAsyncIteration:
    """Tests for async iteration interface."""

    @pytest.mark.asyncio
    async def test_poll_starts_iteration(self, tmp_path, mock_settings):
        """Test that poll() returns an iterator."""
        mock_settings.auto_review_poll_interval = 0.01
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
            settings=mock_settings,
        )

        iterator = await poller.poll()
        assert iterator is poller
        assert poller._running is True

    @pytest.mark.asyncio
    async def test_stop_ends_iteration(self, tmp_path, mock_settings):
        """Test that stop() ends iteration."""
        mock_settings.auto_review_poll_interval = 0.01
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
            settings=mock_settings,
        )

        await poller.poll()
        poller.stop()
        assert poller._running is False

        with pytest.raises(StopAsyncIteration):
            await poller.__anext__()

    @pytest.mark.asyncio
    async def test_iteration_polls_at_interval(self, tmp_path, mock_settings):
        """Test that iteration waits for poll interval."""
        mock_settings.auto_review_poll_interval = 0.5
        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test",
            settings=mock_settings,
        )

        await poller.poll()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await poller.__anext__()

        mock_sleep.assert_awaited_once_with(0.5)
        assert result == []

        poller.stop()

    @pytest.mark.asyncio
    async def test_async_for_loop(self, tmp_path, mock_settings):
        """Test using poller in async for loop."""
        mock_settings.auto_review_poll_interval = 0.01
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        cycle_data = {"cycle": 1, "max_cycles": 3, "verdict": "approved"}
        (step_dir / "review_cycle_1.json").write_text(json.dumps(cycle_data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
            settings=mock_settings,
        )

        results = []
        iteration_count = 0

        async for new_cycles in await poller.poll():
            iteration_count += 1
            results.extend(new_cycles)
            if iteration_count >= 2:  # Stop after 2 iterations
                poller.stop()

        assert len(results) == 1
        assert results[0].verdict == "approved"


# ---------------------------------------------------------------------------
# ReviewCyclePoller reset and processed_count tests
# ---------------------------------------------------------------------------


class TestResetAndProcessedCount:
    """Tests for reset and processed_count methods."""

    @pytest.mark.asyncio
    async def test_processed_count(self, tmp_path):
        """Test processed_count tracks processed files."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        for i in range(3):
            data = {"cycle": i + 1, "max_cycles": 5, "verdict": "approved"}
            (step_dir / f"review_cycle_{i + 1}.json").write_text(json.dumps(data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )

        assert poller.processed_count == 0

        await poller.poll_once()
        assert poller.processed_count == 3

    @pytest.mark.asyncio
    async def test_reset_clears_processed(self, tmp_path):
        """Test that reset clears processed files."""
        step_dir = tmp_path / ".forge" / "test_step"
        step_dir.mkdir(parents=True)

        data = {"cycle": 1, "max_cycles": 3, "verdict": "approved"}
        (step_dir / "review_cycle_1.json").write_text(json.dumps(data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="test_step",
        )

        # First poll
        result1 = await poller.poll_once()
        assert len(result1) == 1
        assert poller.processed_count == 1

        # Reset and poll again
        poller.reset()
        assert poller.processed_count == 0

        result2 = await poller.poll_once()
        assert len(result2) == 1


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


class TestPollerIntegration:
    """Integration-style tests for the poller."""

    @pytest.mark.asyncio
    async def test_detects_files_within_time_limit(self, tmp_path, mock_settings):
        """Test that new files are detected within acceptable time."""
        mock_settings.auto_review_poll_interval = 0.1  # Fast polling for test
        step_dir = tmp_path / ".forge" / "implement_task"
        step_dir.mkdir(parents=True)

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="implement_task",
            settings=mock_settings,
        )

        # Start polling
        await poller.poll()

        # Create file after starting poll
        cycle_data = {
            "cycle": 1,
            "max_cycles": 3,
            "verdict": "rejected",
            "feedback": "Tests failing",
            "skill": "local-code-review",
            "elapsed_seconds": 15.2,
            "timestamp": "2024-01-15T12:00:00Z",
        }
        (step_dir / "review_cycle_1.json").write_text(json.dumps(cycle_data))

        # Poll and check detection
        detected = []
        for _ in range(5):  # Max 5 iterations
            result = await poller.__anext__()
            detected.extend(result)
            if detected:
                break

        poller.stop()

        assert len(detected) == 1
        assert detected[0].cycle == 1
        assert detected[0].verdict == "rejected"
        assert detected[0].feedback == "Tests failing"

    @pytest.mark.asyncio
    async def test_multiple_cycle_files_in_order(self, tmp_path):
        """Test processing multiple review cycles in order."""
        step_dir = tmp_path / ".forge" / "implement_task"
        step_dir.mkdir(parents=True)

        # Create multiple cycle files
        for i in range(1, 4):
            verdict = "approved" if i == 3 else "rejected"
            data = {
                "cycle": i,
                "max_cycles": 3,
                "verdict": verdict,
                "feedback": f"Feedback for cycle {i}" if verdict == "rejected" else "",
                "skill": "local-code-review",
                "elapsed_seconds": i * 5.0,
                "timestamp": f"2024-01-15T10:{i:02d}:00Z",
            }
            (step_dir / f"review_cycle_{i}.json").write_text(json.dumps(data))

        poller = ReviewCyclePoller(
            workspace_path=tmp_path,
            step_name="implement_task",
        )

        result = await poller.poll_once()

        assert len(result) == 3
        assert result[0].cycle == 1
        assert result[0].verdict == "rejected"
        assert result[1].cycle == 2
        assert result[1].verdict == "rejected"
        assert result[2].cycle == 3
        assert result[2].verdict == "approved"


# ---------------------------------------------------------------------------
# Fixture for mock_settings
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings():
    """Create mock settings for tests."""
    return Settings(
        redis_url="redis://localhost:6379/0",
        jira_base_url="https://test.atlassian.net",
        jira_api_token="test-token",
        jira_user_email="test@example.com",
        github_token="test-github-token",
        anthropic_api_key="test-anthropic-key",
        auto_review_poll_interval=5.0,
    )
