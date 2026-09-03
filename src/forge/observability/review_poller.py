"""Review cycle poller for observability during container execution.

This module provides the ReviewCyclePoller class that implements async polling
for review cycle files written by container agents during task execution.

The poller detects files at the step-specific path:
    .forge/{step-name}/review_cycle_*.json

where step-name (e.g., "implement_task", "generate_prd") is passed when
creating the poller instance.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from forge.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Maximum retries for JSON parsing (race condition with container writes)
MAX_JSON_PARSE_RETRIES = 3
# Delay between JSON parse retries in seconds
JSON_PARSE_RETRY_DELAY = 0.5


@dataclass
class ReviewCycleData:
    """Data captured for a single review cycle iteration.

    This mirrors the ReviewCycleData from containers.review for use in
    the orchestrator-side polling.

    Attributes:
        cycle: Current cycle number (1-indexed).
        max_cycles: Maximum cycles allowed.
        verdict: Review outcome ("approved" or "rejected").
        feedback: Reviewer feedback text.
        skill: Name of the skill that performed the review.
        elapsed_seconds: Time taken for this review cycle.
        timestamp: ISO 8601 UTC timestamp of cycle completion.
        file_path: Path to the source JSON file (not serialized).
    """

    cycle: int
    max_cycles: int
    verdict: str
    feedback: str
    skill: str
    elapsed_seconds: float
    timestamp: str
    file_path: str = ""

    @classmethod
    def from_dict(cls, data: dict, file_path: str = "") -> "ReviewCycleData":
        """Create ReviewCycleData from a dictionary.

        Args:
            data: Dictionary containing review cycle fields.
            file_path: Path to the source file for tracking.

        Returns:
            ReviewCycleData instance.
        """
        return cls(
            cycle=data["cycle"],
            max_cycles=data["max_cycles"],
            verdict=data["verdict"],
            feedback=data.get("feedback", ""),
            skill=data.get("skill", ""),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            timestamp=data.get("timestamp", ""),
            file_path=file_path,
        )


class ReviewCyclePoller:
    """Async poller for review cycle files during container execution.

    This class polls for review_cycle_*.json files in the step-specific
    directory and returns newly detected ReviewCycleData objects.

    Usage:
        poller = ReviewCyclePoller(
            workspace_path=Path("/workspace"),
            step_name="implement_work",
        )

        # Start polling in background
        async for new_cycles in poller.poll():
            for cycle in new_cycles:
                print(f"Review cycle {cycle.cycle}: {cycle.verdict}")

        # Or poll once
        new_cycles = await poller.poll_once()
    """

    def __init__(
        self,
        workspace_path: Path,
        step_name: str,
        task_key: str = "",
        skill_name: str = "",
        settings: Settings | None = None,
    ):
        """Initialize the review cycle poller.

        Args:
            workspace_path: Path to the workspace root (where .forge/ is located).
            step_name: Name of the step (e.g., "implement_work") for metrics.
            task_key: Jira task key (e.g., "AISOS-2126") for directory naming.
            skill_name: Skill name (e.g., "implement-task") for directory naming.
            settings: Application settings. Uses default if not provided.
        """
        self.workspace_path = Path(workspace_path)
        self.step_name = step_name
        self.task_key = task_key
        self.skill_name = skill_name
        self._settings = settings
        self._processed_files: set[str] = set()
        self._running = False

    @property
    def settings(self) -> Settings:
        """Get settings, lazily loading if not provided."""
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    @property
    def poll_interval(self) -> float:
        """Get the polling interval from settings."""
        return self.settings.auto_review_poll_interval

    @staticmethod
    def build_cycle_dir(
        workspace_path: Path, task_key: str, skill_name: str, step_name: str
    ) -> Path:
        """Build the directory path for review cycle files."""
        if task_key and skill_name:
            return workspace_path / ".forge" / "reviews" / f"{task_key}__{skill_name}"
        return workspace_path / ".forge" / step_name

    @property
    def review_cycle_dir(self) -> Path:
        """Get the directory path for review cycle files."""
        return self.build_cycle_dir(
            self.workspace_path, self.task_key, self.skill_name, self.step_name
        )

    def _get_review_cycle_files(self) -> list[Path]:
        """Get list of review_cycle_*.json files in the step directory.

        Returns:
            List of Path objects for matching files.
        """
        cycle_dir = self.review_cycle_dir
        if not cycle_dir.exists():
            return []

        return sorted(cycle_dir.glob("review_cycle_*.json"))

    async def _parse_json_with_retry(self, file_path: Path) -> dict | None:
        """Parse JSON file with retry for incomplete reads.

        This handles race conditions where the container may still be
        writing the file when we try to read it.

        Args:
            file_path: Path to the JSON file.

        Returns:
            Parsed JSON dict, or None if parsing fails after retries.
        """
        for attempt in range(MAX_JSON_PARSE_RETRIES):
            try:
                content = file_path.read_text(encoding="utf-8")

                if not content.strip():
                    # Empty file, likely still being written
                    if attempt < MAX_JSON_PARSE_RETRIES - 1:
                        logger.debug(
                            "Empty file %s, retrying (%d/%d)",
                            file_path,
                            attempt + 1,
                            MAX_JSON_PARSE_RETRIES,
                        )
                        await asyncio.sleep(JSON_PARSE_RETRY_DELAY)
                        continue
                    return None

                return json.loads(content)

            except json.JSONDecodeError as e:
                if attempt < MAX_JSON_PARSE_RETRIES - 1:
                    logger.debug(
                        "JSON parse error for %s: %s, retrying (%d/%d)",
                        file_path,
                        e,
                        attempt + 1,
                        MAX_JSON_PARSE_RETRIES,
                    )
                    await asyncio.sleep(JSON_PARSE_RETRY_DELAY)
                else:
                    logger.warning(
                        "Failed to parse %s after %d attempts: %s",
                        file_path,
                        MAX_JSON_PARSE_RETRIES,
                        e,
                    )
                    return None

            except OSError as e:
                logger.warning("Error reading %s: %s", file_path, e)
                return None

        return None

    async def poll_once(self) -> list[ReviewCycleData]:
        """Poll for new review cycle files once.

        Returns:
            List of newly detected ReviewCycleData objects.
        """
        new_cycles: list[ReviewCycleData] = []
        files = self._get_review_cycle_files()

        for file_path in files:
            file_key = str(file_path)

            if file_key in self._processed_files:
                continue

            data = await self._parse_json_with_retry(file_path)
            if data is None:
                continue

            try:
                cycle_data = ReviewCycleData.from_dict(data, file_path=file_key)
                new_cycles.append(cycle_data)
                self._processed_files.add(file_key)
                logger.debug(
                    "Detected review cycle %d for step %s: %s",
                    cycle_data.cycle,
                    self.step_name,
                    cycle_data.verdict,
                )
            except (KeyError, TypeError) as e:
                logger.warning("Invalid review cycle data in %s: %s", file_path, e)

        return new_cycles

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    async def run_loop(self, callback) -> None:
        """Poll for review cycle files, calling callback(cycles) on each batch.

        Runs until stop() is called or the task is cancelled.
        """
        self._running = True
        while self._running:
            await asyncio.sleep(self.poll_interval)
            if not self._running:
                break
            new_cycles = await self.poll_once()
            if new_cycles:
                callback(new_cycles)
