"""Unit tests for Getting Started documentation note integrity."""

import subprocess
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


def is_path_in_allowed_directories(path_str: str) -> bool:
    """Check if the given relative path is within the allowed directories (docs/ or tests/)."""
    # Normalize path separator to forward slash
    path_str = path_str.replace("\\", "/")
    return path_str.startswith("docs/") or path_str.startswith("tests/")


def parse_git_status_line(line: str) -> list[str]:
    """Parse a single line from 'git status --porcelain' and return the file path(s) involved."""
    if len(line) < 4:
        return []
    # In porcelain format, the status is the first 2 characters, followed by a space.
    status = line[:2]
    path_part = line[3:]

    # Handle rename/copy which has " -> " separator
    if ("R" in status or "C" in status) and " -> " in path_part:
        parts = path_part.split(" -> ")
        return [p.strip("\"' ") for p in parts]

    return [path_part.strip("\"' ")]


def check_exclusive_documentation_scope(git_status_output: str) -> None:
    """Verifies that all modified or staged files are restricted to docs/ and tests/ directories.

    Raises AssertionError if any file outside of docs/ and tests/ directories is modified or staged.
    """
    violating_files = []
    for line in git_status_output.splitlines():
        if not line.strip():
            continue
        paths = parse_git_status_line(line)
        for path in paths:
            if not is_path_in_allowed_directories(path):
                violating_files.append((line, path))

    if violating_files:
        details = "\n".join(
            f"- Line: '{line.strip()}' (Parsed Path: '{path}')" for line, path in violating_files
        )
        raise AssertionError(
            f"Business Rule BR-002 Violation: Changes detected outside of docs/ and tests/ directories.\n"
            f"The following violating files were modified or staged:\n{details}"
        )


def test_exclusive_documentation_scope() -> None:
    """Verify that no files outside of docs/ and tests/ directories have been modified or staged (BR-002)."""
    # Execute git status --porcelain
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )

    # Verify using our validator
    check_exclusive_documentation_scope(result.stdout)


def test_exclusive_scope_success_empty() -> None:
    """Verify that empty git status output passes the scope validation."""
    check_exclusive_documentation_scope("")


def test_exclusive_scope_success_docs_and_tests() -> None:
    """Verify that modifications restricted to docs/ and tests/ pass the scope validation."""
    status_output = (
        " M docs/getting-started.md\n"
        "A  tests/unit/test_documentation.py\n"
        "?? tests/unit/new_test_file.py\n"
    )
    check_exclusive_documentation_scope(status_output)


def test_exclusive_scope_failure_src() -> None:
    """Verify that modifications in src/ cause scope validation failure."""
    status_output = " M docs/getting-started.md\n M src/forge/main.py\n"
    with pytest.raises(
        AssertionError,
        match="Business Rule BR-002 Violation: Changes detected outside of docs/ and tests/ directories",
    ) as exc_info:
        check_exclusive_documentation_scope(status_output)

    assert "src/forge/main.py" in str(exc_info.value)


def test_exclusive_scope_failure_config() -> None:
    """Verify that modifications in root configuration files cause scope validation failure."""
    status_output = " M pyproject.toml\n"
    with pytest.raises(
        AssertionError,
        match="Business Rule BR-002 Violation: Changes detected outside of docs/ and tests/ directories",
    ) as exc_info:
        check_exclusive_documentation_scope(status_output)

    assert "pyproject.toml" in str(exc_info.value)


def test_exclusive_scope_rename_success() -> None:
    """Verify that renames restricted to docs/ pass the scope validation."""
    status_output = "R  docs/old-doc.md -> docs/new-doc.md\n"
    check_exclusive_documentation_scope(status_output)


def test_exclusive_scope_rename_failure() -> None:
    """Verify that renames moving files outside of docs/ and tests/ cause scope validation failure."""
    status_output = "R  docs/getting-started.md -> src/getting-started.md\n"
    with pytest.raises(
        AssertionError,
        match="Business Rule BR-002 Violation: Changes detected outside of docs/ and tests/ directories",
    ) as exc_info:
        check_exclusive_documentation_scope(status_output)

    assert "src/getting-started.md" in str(exc_info.value)


def test_exclusive_scope_rename_with_different_status_codes() -> None:
    """Verify that renames with different status codes (e.g., RM,  R) are parsed correctly."""
    # Renamed in worktree with space prefix " R"
    status_output_1 = " R docs/old-doc.md -> docs/new-doc.md\n"
    check_exclusive_documentation_scope(status_output_1)

    # Renamed and modified "RM"
    status_output_2 = "RM docs/old-doc.md -> docs/new-doc.md\n"
    check_exclusive_documentation_scope(status_output_2)

    # Rename failure with different status code
    status_output_3 = " R docs/getting-started.md -> src/getting-started.md\n"
    with pytest.raises(AssertionError):
        check_exclusive_documentation_scope(status_output_3)


def test_zensical_build_clean_compilation() -> None:
    """Verify that the documentation site compiles cleanly using the Zensical build pipeline."""
    # Run the compilation command: uv run --extra docs zensical build
    root_dir = Path(__file__).parents[2]
    uv_path = root_dir / ".venv" / "bin" / "uv"

    # Try using the absolute path of uv in .venv first if it exists, otherwise fall back to system 'uv'
    if uv_path.exists():
        cmd = [str(uv_path), "run", "--extra", "docs", "zensical", "build"]
    else:
        cmd = ["uv", "run", "--extra", "docs", "zensical", "build"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        pytest.fail("Neither .venv/bin/uv nor system 'uv' was found on the path.")

    # 1. Running uv run --extra docs zensical build returns exit code 0
    assert result.returncode == 0, (
        f"Zensical build failed with exit code {result.returncode}.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    # 2. No compilation warnings or syntax errors are reported during the build.
    stderr_lines = result.stderr.splitlines()
    filtered_stderr = []
    for line in stderr_lines:
        line_lower = line.lower()
        # Exclude typical uv setup / performance / cache / notice messages
        if "failed to hardlink" in line_lower:
            continue
        if "if the cache and target" in line_lower:
            continue
        if "if this is intentional, set" in line_lower or "suppress this warning" in line_lower:
            continue
        if "notice: a new release of pip" in line_lower:
            continue
        # Also exclude virtualenv / downloading output from uv
        if line_lower.startswith("downloading ") or line_lower.startswith(" downloaded "):
            continue
        if "uninstalled" in line_lower and "package" in line_lower:
            continue
        if "installed" in line_lower and "package" in line_lower:
            continue
        if "built forge" in line_lower or "building forge" in line_lower:
            continue
        filtered_stderr.append(line)

    filtered_stderr_str = "\n".join(filtered_stderr)

    assert "error" not in filtered_stderr_str.lower(), (
        f"Zensical build reported errors:\n{filtered_stderr_str}"
    )
    assert "warning" not in filtered_stderr_str.lower(), (
        f"Zensical build reported warnings:\n{filtered_stderr_str}"
    )

    # 3. The built static documentation site is correctly generated under the configured output directory.
    root_dir = Path(__file__).parents[2]
    site_dir = root_dir / "site"
    assert site_dir.exists() and site_dir.is_dir(), (
        f"Built documentation directory '{site_dir}' does not exist or is not a directory."
    )

    getting_started_index = site_dir / "getting-started" / "index.html"
    assert getting_started_index.exists() and getting_started_index.is_file(), (
        f"Built documentation for getting-started is missing: '{getting_started_index}'"
    )

    # Verify that the cat troubleshooting note is in getting_started_index
    content = getting_started_index.read_text(encoding="utf-8")
    assert "🐱" in content, "The Unicode cat emoji '🐱' was not found in the compiled HTML."
    assert "HTTP 405 error" in content, (
        "The troubleshooting note text was not found in the compiled HTML."
    )
