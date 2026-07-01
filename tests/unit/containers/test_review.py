"""Unit tests for containers.review module."""

from pathlib import Path

import pytest
from containers.review import (
    DEFAULT_MAX_RETRIES,
    ENV_MAX_RETRIES,
    ReviewConfig,
    ReviewCycleData,
    Verdict,
    parse_review_config,
)

# ---------------------------------------------------------------------------
# Verdict enum tests
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_approved_value(self):
        assert Verdict.APPROVED == "approved"
        assert Verdict.APPROVED.value == "approved"

    def test_rejected_value(self):
        assert Verdict.REJECTED == "rejected"
        assert Verdict.REJECTED.value == "rejected"

    def test_is_str_enum(self):
        # StrEnum values can be used directly as strings
        assert f"Verdict: {Verdict.APPROVED}" == "Verdict: approved"


# ---------------------------------------------------------------------------
# ReviewConfig dataclass tests
# ---------------------------------------------------------------------------


class TestReviewConfig:
    def test_default_values(self):
        config = ReviewConfig()
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert config.instructions == ""

    def test_custom_values(self):
        config = ReviewConfig(max_retries=5, instructions="Check for bugs")
        assert config.max_retries == 5
        assert config.instructions == "Check for bugs"

    def test_default_max_retries_is_3(self):
        assert DEFAULT_MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# ReviewCycleData dataclass tests
# ---------------------------------------------------------------------------


class TestReviewCycleData:
    def test_all_fields_required(self):
        data = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict="approved",
            feedback="Looks good!",
            skill="code-review",
            elapsed_seconds=12.5,
            timestamp="2024-01-15T10:30:00Z",
        )
        assert data.cycle == 1
        assert data.max_cycles == 3
        assert data.verdict == "approved"
        assert data.feedback == "Looks good!"
        assert data.skill == "code-review"
        assert data.elapsed_seconds == 12.5
        assert data.timestamp == "2024-01-15T10:30:00Z"

    def test_verdict_can_be_approved_or_rejected(self):
        approved = ReviewCycleData(
            cycle=1,
            max_cycles=3,
            verdict=Verdict.APPROVED,
            feedback="",
            skill="test",
            elapsed_seconds=1.0,
            timestamp="2024-01-01T00:00:00Z",
        )
        assert approved.verdict == "approved"

        rejected = ReviewCycleData(
            cycle=2,
            max_cycles=3,
            verdict=Verdict.REJECTED,
            feedback="Needs work",
            skill="test",
            elapsed_seconds=2.0,
            timestamp="2024-01-01T00:00:00Z",
        )
        assert rejected.verdict == "rejected"


# ---------------------------------------------------------------------------
# parse_review_config tests
# ---------------------------------------------------------------------------


class TestParseReviewConfig:
    """Tests for parse_review_config function."""

    def test_parse_valid_frontmatter(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: 5
---
Review the code for security issues.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == 5
        assert config.instructions == "Review the code for security issues."

    def test_parse_only_instructions_no_frontmatter(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text("Just some instructions, no frontmatter.")

        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert config.instructions == "Just some instructions, no frontmatter."

    def test_parse_empty_frontmatter(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
---
Instructions after empty frontmatter.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert config.instructions == "Instructions after empty frontmatter."

    def test_file_not_found_returns_defaults(self, tmp_path: Path):
        review_md = tmp_path / "nonexistent.md"
        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert config.instructions == ""

    def test_malformed_yaml_logs_warning_and_returns_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: [invalid yaml
---
Some instructions.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert config.instructions == "Some instructions."
        assert "Malformed YAML" in caplog.text

    def test_env_var_fallback_when_no_frontmatter_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_MAX_RETRIES, "7")

        review_md = tmp_path / "review.md"
        review_md.write_text("No frontmatter, just instructions.")

        config = parse_review_config(review_md)
        assert config.max_retries == 7
        assert config.instructions == "No frontmatter, just instructions."

    def test_frontmatter_overrides_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_MAX_RETRIES, "7")

        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: 2
---
Instructions here.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == 2

    def test_invalid_env_var_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv(ENV_MAX_RETRIES, "not-a-number")

        review_md = tmp_path / "review.md"
        review_md.write_text("Instructions only.")

        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert "Invalid AUTO_REVIEW_MAX_RETRIES" in caplog.text

    def test_non_integer_max_retries_in_frontmatter_uses_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: "five"
---
Instructions.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == DEFAULT_MAX_RETRIES
        assert "Invalid max_retries" in caplog.text

    def test_multiline_instructions(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: 4
---
Line one.
Line two.
Line three.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == 4
        assert "Line one." in config.instructions
        assert "Line two." in config.instructions
        assert "Line three." in config.instructions

    def test_frontmatter_with_extra_fields_ignored(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: 6
author: test
some_other_field: value
---
Instructions.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == 6
        assert config.instructions == "Instructions."

    def test_env_var_fallback_for_file_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_MAX_RETRIES, "10")

        review_md = tmp_path / "nonexistent.md"
        config = parse_review_config(review_md)
        assert config.max_retries == 10
        assert config.instructions == ""

    def test_env_var_fallback_for_malformed_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_MAX_RETRIES, "8")

        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
: invalid: yaml:
---
Some instructions.
"""
        )
        config = parse_review_config(review_md)
        assert config.max_retries == 8
        assert config.instructions == "Some instructions."

    def test_no_trailing_newline_after_frontmatter(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text("---\nmax_retries: 3\n---\nInstructions.")
        config = parse_review_config(review_md)
        assert config.max_retries == 3
        assert config.instructions == "Instructions."

    def test_whitespace_handling_in_instructions(self, tmp_path: Path):
        review_md = tmp_path / "review.md"
        review_md.write_text(
            """\
---
max_retries: 1
---

   Indented instructions with leading/trailing whitespace.

"""
        )
        config = parse_review_config(review_md)
        # Instructions should be stripped
        assert config.instructions == "Indented instructions with leading/trailing whitespace."
