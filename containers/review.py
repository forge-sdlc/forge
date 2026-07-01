"""Review loop data structures for the container review engine.

This module provides core data structures used by the review loop engine:

- ReviewConfig: Configuration parsed from review.md frontmatter
- ReviewCycleData: Data captured for each review cycle iteration
- Verdict: Enum for review outcomes (APPROVED/REJECTED)
- parse_review_config: Parser for review.md YAML frontmatter
"""

import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Default max retries for review loops
DEFAULT_MAX_RETRIES = 3

# Environment variable for max retries override
ENV_MAX_RETRIES = "AUTO_REVIEW_MAX_RETRIES"


class Verdict(StrEnum):
    """Review verdict indicating approval or rejection."""

    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class ReviewConfig:
    """Configuration for review loops parsed from review.md frontmatter.

    Attributes:
        max_retries: Maximum number of retry attempts (default: 3).
        instructions: Review instructions from the markdown body.
    """

    max_retries: int = DEFAULT_MAX_RETRIES
    instructions: str = ""


@dataclass
class ReviewCycleData:
    """Data captured for a single review cycle iteration.

    Attributes:
        cycle: Current cycle number (1-indexed).
        max_cycles: Maximum cycles allowed.
        verdict: Review outcome ("approved" or "rejected").
        feedback: Reviewer feedback text.
        skill: Name of the skill that performed the review.
        elapsed_seconds: Time taken for this review cycle.
        timestamp: ISO 8601 UTC timestamp of cycle completion.
    """

    cycle: int
    max_cycles: int
    verdict: str
    feedback: str
    skill: str
    elapsed_seconds: float
    timestamp: str


# Regex pattern for YAML frontmatter: --- at start, optional content, --- delimiter
# The frontmatter content between delimiters may be empty (just newlines)
_FRONTMATTER_PATTERN = re.compile(
    r"\A---\s*\n(.*?)---\s*\n?(.*)",
    re.DOTALL,
)


def parse_review_config(review_md_path: Path) -> ReviewConfig:
    """Parse ReviewConfig from a review.md file with YAML frontmatter.

    The file format is:
    ```
    ---
    max_retries: 5
    ---
    Review instructions here...
    ```

    Priority for max_retries:
    1. YAML frontmatter value
    2. AUTO_REVIEW_MAX_RETRIES environment variable
    3. Default value (3)

    If the file does not exist or YAML parsing fails, returns defaults
    with a warning log (per BR-006).

    Args:
        review_md_path: Path to the review.md file.

    Returns:
        ReviewConfig with parsed values or defaults.
    """
    # Get env var fallback for max_retries
    env_max_retries: int | None = None
    env_value = os.environ.get(ENV_MAX_RETRIES)
    if env_value is not None:
        try:
            env_max_retries = int(env_value)
        except ValueError:
            logger.warning(
                "Invalid %s value %r, ignoring",
                ENV_MAX_RETRIES,
                env_value,
            )

    # Default config to return on errors
    default_max_retries = env_max_retries if env_max_retries is not None else DEFAULT_MAX_RETRIES

    if not review_md_path.exists():
        logger.warning("Review config not found at %s, using defaults", review_md_path)
        return ReviewConfig(max_retries=default_max_retries, instructions="")

    try:
        content = review_md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to read %s: %s, using defaults", review_md_path, e)
        return ReviewConfig(max_retries=default_max_retries, instructions="")

    # Try to parse frontmatter
    match = _FRONTMATTER_PATTERN.match(content)
    if not match:
        # No frontmatter found - treat entire content as instructions
        return ReviewConfig(max_retries=default_max_retries, instructions=content.strip())

    frontmatter_raw = match.group(1)
    instructions = match.group(2).strip()

    # Parse YAML frontmatter
    try:
        frontmatter = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError as e:
        logger.warning(
            "Malformed YAML frontmatter in %s: %s, using defaults",
            review_md_path,
            e,
        )
        return ReviewConfig(max_retries=default_max_retries, instructions=instructions)

    # Handle empty frontmatter
    if frontmatter is None:
        frontmatter = {}

    # Extract max_retries with fallback chain
    max_retries = default_max_retries
    if isinstance(frontmatter, dict) and "max_retries" in frontmatter:
        fm_value = frontmatter["max_retries"]
        if isinstance(fm_value, int):
            max_retries = fm_value
        else:
            logger.warning(
                "Invalid max_retries value %r in %s, using default",
                fm_value,
                review_md_path,
            )

    return ReviewConfig(max_retries=max_retries, instructions=instructions)
