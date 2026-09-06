"""Review cycle recorder for observability of review cycle data.

This module provides the ReviewCycleRecorder class that records review cycle
data either by logging or by copying files to a recording directory.

Modes:
    - mode="log": Log cycle data at INFO level via logging
    - mode="copy": Copy files to {recording_dir}/{step-name}/review_cycle_*.json
    - mode=None: No recording (disabled)

The step name is passed to the recorder constructor and used to organize
recorded files into step-specific subdirectories.
"""

import logging
import shutil
from pathlib import Path
from typing import Literal

from forge.observability.review_poller import ReviewCycleData

logger = logging.getLogger(__name__)

# Type alias for recording modes
RecordingMode = Literal["log", "copy"] | None


class ReviewCycleRecorder:
    """Records review cycle data based on configured mode.

    This class handles recording of review cycle data either by logging
    to stdout/file or by copying files to a recording directory.

    Usage:
        recorder = ReviewCycleRecorder(
            step_name="implement_work",
            mode="log",
        )
        recorder.record(cycle_data)

        # Or for copy mode
        recorder = ReviewCycleRecorder(
            step_name="implement_work",
            mode="copy",
            recording_dir=Path("/recordings"),
        )
        recorder.record_file(Path("/workspace/.forge/step/review_cycle_1.json"))
    """

    def __init__(
        self,
        step_name: str,
        mode: RecordingMode = None,
        recording_dir: Path | None = None,
    ):
        """Initialize the review cycle recorder.

        Args:
            step_name: Name of the step (e.g., "implement_work") for file organization.
            mode: Recording mode - "log", "copy", or None (disabled).
            recording_dir: Base directory for copying files (required for copy mode).

        Raises:
            ValueError: If mode is "copy" but recording_dir is not provided.
        """
        self.step_name = step_name
        self.mode = mode
        self.recording_dir = Path(recording_dir) if recording_dir else None

        if mode == "copy" and not self.recording_dir:
            raise ValueError("recording_dir is required when mode='copy'")

    @property
    def step_dir(self) -> Path | None:
        """Get the step-specific directory for recorded files.

        Returns:
            Path to {recording_dir}/{step-name}/ or None if no recording_dir.
        """
        if self.recording_dir is None:
            return None
        return self.recording_dir / self.step_name

    def record(self, cycle_data: ReviewCycleData) -> None:
        """Record review cycle data based on configured mode.

        For log mode, logs the data at INFO level with structured format.
        For copy mode, this method does nothing (use record_file for files).
        For disabled mode (None), this method does nothing.

        Args:
            cycle_data: The review cycle data to record.
        """
        if self.mode is None:
            return

        if self.mode == "log":
            self._log_cycle_data(cycle_data)
        # Copy mode is handled by record_file

    def record_file(self, source_file: Path) -> Path | None:
        """Record a review cycle file by copying it to the recording directory.

        Only works in copy mode. Creates the step-specific subdirectory if needed.

        Args:
            source_file: Path to the source review_cycle_*.json file.

        Returns:
            Path to the copied file, or None if mode is not "copy" or copy failed.
        """
        if self.mode != "copy":
            return None

        if not self.recording_dir:
            return None

        if not source_file.exists():
            logger.warning("Source file does not exist: %s", source_file)
            return None

        # Create step-specific subdirectory
        step_dir = self.step_dir
        if step_dir is None:
            return None

        try:
            step_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("Failed to create directory %s: %s", step_dir, e)
            return None

        # Copy file preserving the filename
        dest_file = step_dir / source_file.name
        try:
            shutil.copy2(source_file, dest_file)
            logger.debug("Copied review cycle file to %s", dest_file)
            return dest_file
        except OSError as e:
            logger.error("Failed to copy file to %s: %s", dest_file, e)
            return None

    def _log_cycle_data(self, cycle_data: ReviewCycleData) -> None:
        """Log review cycle data at INFO level with structured format.

        Args:
            cycle_data: The review cycle data to log.
        """
        logger.info(
            "Review cycle %d/%d for %s: verdict=%s skill=%s elapsed=%.2fs feedback=%r",
            cycle_data.cycle,
            cycle_data.max_cycles,
            self.step_name,
            cycle_data.verdict,
            cycle_data.skill,
            cycle_data.elapsed_seconds,
            cycle_data.feedback[:100] + "..."
            if len(cycle_data.feedback) > 100
            else cycle_data.feedback,
        )
