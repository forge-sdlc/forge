"""Unit tests for Getting Started documentation note integrity."""

from pathlib import Path

import pytest


def extract_tab_content(md_content: str, tab_title: str) -> str | None:
    """Extracts the content of a specific tab section from Markdown.

    A tab section starts with a line containing `=== "tab_title"`.
    All subsequent lines belonging to this tab are indented (by at least 4 spaces)
    or are blank lines. The tab section ends when a non-blank line with no leading
    indentation (spaces or tabs) is encountered.
    """
    lines = md_content.splitlines()
    in_tab = False
    tab_lines = []
    for line in lines:
        if not in_tab:
            if line.strip().startswith(f'=== "{tab_title}"'):
                in_tab = True
                continue
        else:
            # End of tab is a non-empty line starting with no spaces/tabs (e.g. another tab or heading)
            if line.strip() != "" and not (line.startswith(" ") or line.startswith("\t")):
                break
            tab_lines.append(line)

    if not in_tab:
        return None
    return "\n".join(tab_lines)


def find_troubleshooting_note(tab_content: str) -> str | None:
    """Finds the line containing the troubleshooting note in the extracted tab content.

    We locate the note by looking for the core text phrase 'HTTP 405 error'.
    This allows us to find the note even if the emoji or other properties are incorrect
    or missing, so that we can perform assertions on those properties.
    """
    for line in tab_content.splitlines():
        stripped = line.strip()
        if "HTTP 405 error" in stripped:
            return stripped
    return None


def validate_troubleshooting_note(md_content: str) -> None:
    """Validates the markdown content of getting-started.md for the troubleshooting note.

    Raises AssertionError if any of the documentation integrity rules are violated.
    """
    # 1. Extract content of === "forge-poller (recommended)"
    tab_content = extract_tab_content(md_content, "forge-poller (recommended)")
    assert tab_content is not None, (
        "Section === 'forge-poller (recommended)' was not found in the documentation."
    )

    # 2. Find the troubleshooting note in the extracted tab content
    note = find_troubleshooting_note(tab_content)
    assert note is not None, (
        "The troubleshooting note was not found inside the 'forge-poller (recommended)' tab section."
    )

    # 3. Verify it starts with the 🐱 Unicode cat emoji
    assert note.startswith("🐱"), (
        "The troubleshooting note must start with a 🐱 Unicode cat emoji (BR-003)."
    )

    # 4. Verify it is exactly one sentence in length (no intermediate periods)
    text_content = note[1:].strip()
    assert text_content.endswith("."), "The troubleshooting note must end with a period."
    assert "." not in text_content[:-1], (
        "The troubleshooting note must be exactly one sentence in length (no intermediate sentence-ending periods) (BR-001)."
    )


def test_getting_started_exists() -> None:
    """Verify that the docs/getting-started.md file exists."""
    doc_path = Path(__file__).parents[2] / "docs" / "getting-started.md"
    assert doc_path.exists(), "docs/getting-started.md file does not exist"
    assert doc_path.is_file(), "docs/getting-started.md is not a file"


def test_getting_started_note_integrity() -> None:
    """Verify the integrity of the troubleshooting note in the actual docs/getting-started.md file."""
    doc_path = Path(__file__).parents[2] / "docs" / "getting-started.md"
    md_content = doc_path.read_text(encoding="utf-8")
    validate_troubleshooting_note(md_content)


def test_validation_failure_missing_tab() -> None:
    """Verify that validation fails if the recommended tab section is missing."""
    bad_md = """
For local development you have two options:

=== "forge-poller (not recommended)"

    Register the ticket you're testing:

    🐱 If you receive an HTTP 405 error, you likely sent the request to the Forge gateway port (8000) instead of the forge-poller port (8001).
"""
    with pytest.raises(
        AssertionError, match="Section === 'forge-poller \\(recommended\\)' was not found"
    ):
        validate_troubleshooting_note(bad_md)


def test_validation_failure_missing_note() -> None:
    """Verify that validation fails if the troubleshooting note is missing inside the recommended tab."""
    bad_md = """
For local development you have two options:

=== "forge-poller (recommended)"

    Register the ticket you're testing:
"""
    with pytest.raises(
        AssertionError,
        match="The troubleshooting note was not found inside the 'forge-poller \\(recommended\\)'",
    ):
        validate_troubleshooting_note(bad_md)


def test_validation_failure_missing_emoji() -> None:
    """Verify that validation fails if the 🐱 Unicode cat emoji is missing from the troubleshooting note."""
    bad_md = """
For local development you have two options:

=== "forge-poller (recommended)"

    Register the ticket you're testing:

    If you receive an HTTP 405 error, you likely sent the request to the Forge gateway port (8000) instead of the forge-poller port (8001).
"""
    with pytest.raises(
        AssertionError, match="The troubleshooting note must start with a 🐱 Unicode cat emoji"
    ):
        validate_troubleshooting_note(bad_md)


def test_validation_failure_not_one_sentence() -> None:
    """Verify that validation fails if the troubleshooting note contains multiple sentences."""
    bad_md = """
For local development you have two options:

=== "forge-poller (recommended)"

    Register the ticket you're testing:

    🐱 If you receive an HTTP 405 error. You likely sent the request to the Forge gateway port (8000) instead of the forge-poller port (8001).
"""
    with pytest.raises(
        AssertionError, match="The troubleshooting note must be exactly one sentence in length"
    ):
        validate_troubleshooting_note(bad_md)


def test_validation_failure_placed_incorrectly() -> None:
    """Verify that validation fails if the troubleshooting note is placed in the wrong section."""
    bad_md = """
For local development you have two options:

=== "forge-poller (recommended)"

    Register the ticket you're testing:

=== "ngrok (tunnel)"

    🐱 If you receive an HTTP 405 error, you likely sent the request to the Forge gateway port (8000) instead of the forge-poller port (8001).
"""
    with pytest.raises(
        AssertionError,
        match="The troubleshooting note was not found inside the 'forge-poller \\(recommended\\)'",
    ):
        validate_troubleshooting_note(bad_md)
