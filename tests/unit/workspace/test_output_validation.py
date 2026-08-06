"""Tests for the trusted repository-output validation gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.workspace.output_validation import (
    OutputValidationError,
    OutputValidationPolicy,
    validate_repository_output,
)


def _run(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _run(tmp_path, "clone", str(origin), str(work))
    _run(work, "config", "user.email", "forge@example.com")
    _run(work, "config", "user.name", "Forge")
    (work / "README.md").write_text("initial\n")
    _run(work, "add", "README.md")
    _run(work, "commit", "-m", "initial")
    _run(work, "push", "-u", "origin", "main")
    _run(work, "remote", "set-head", "origin", "main")
    _run(work, "switch", "-c", "forge/task-1")
    return work


def _commit(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _run(repo, "add", path)
    _run(repo, "commit", "-m", f"change {path}")


def test_accepts_bounded_regular_output(repository: Path) -> None:
    _commit(repository, "src/example.py", "print('safe')\n")

    result = validate_repository_output(repository, OutputValidationPolicy())

    assert result.changed_paths == ("src/example.py",)
    assert result.validators == ("safe_repository_output",)


@pytest.mark.parametrize("path", [".github/workflows/release.yml", "CODEOWNERS"])
def test_rejects_protected_path(repository: Path, path: str) -> None:
    _commit(repository, path, "unsafe\n")

    with pytest.raises(OutputValidationError, match="protected path changed"):
        validate_repository_output(
            repository,
            OutputValidationPolicy(protected_paths=(".github/workflows/**", "CODEOWNERS")),
        )


def test_rejects_symlink_output(repository: Path) -> None:
    (repository / "escape").symlink_to("/etc/passwd")
    _run(repository, "add", "escape")
    _run(repository, "commit", "-m", "add link")

    with pytest.raises(OutputValidationError, match="symbolic link"):
        validate_repository_output(repository, OutputValidationPolicy(reject_symlinks=True))


def test_unconfigured_policy_does_not_reject_symlink_or_size(repository: Path) -> None:
    (repository / "large-link").symlink_to("README.md")
    _run(repository, "add", "large-link")
    _run(repository, "commit", "-m", "add link")

    result = validate_repository_output(repository, OutputValidationPolicy())

    assert result.changed_paths == ("large-link",)


def test_trusted_repository_policy_can_reject_symlinks(repository: Path) -> None:
    _run(repository, "switch", "main")
    _commit(
        repository,
        ".forge-output-policy.yml",
        "version: 1\nreject_symlinks: true\n",
    )
    _run(repository, "push", "origin", "main")
    _run(repository, "switch", "forge/task-1")
    _run(repository, "rebase", "main")
    (repository / "output-link").symlink_to("README.md")
    _run(repository, "add", "output-link")
    _run(repository, "commit", "-m", "add link")

    with pytest.raises(OutputValidationError, match="symbolic link"):
        validate_repository_output(
            repository, OutputValidationPolicy(), base_ref="origin/main"
        )


def test_rejects_oversized_file(repository: Path) -> None:
    _commit(repository, "large.txt", "12345")

    with pytest.raises(OutputValidationError, match="file exceeds 4 bytes"):
        validate_repository_output(repository, OutputValidationPolicy(max_file_bytes=4))


def test_rejects_oversized_combined_output(repository: Path) -> None:
    _commit(repository, "one.txt", "123")
    _commit(repository, "two.txt", "456")

    with pytest.raises(OutputValidationError, match="changed output exceeds 5 bytes"):
        validate_repository_output(repository, OutputValidationPolicy(max_total_bytes=5))


def test_fails_closed_when_remote_default_branch_is_unknown(repository: Path) -> None:
    _commit(repository, "safe.txt", "safe")
    _run(repository, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    with pytest.raises(OutputValidationError, match="default branch is unavailable"):
        validate_repository_output(repository, OutputValidationPolicy())


def test_configured_base_works_without_remote_head(repository: Path) -> None:
    _commit(repository, "safe.txt", "safe")
    _run(repository, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    result = validate_repository_output(
        repository, OutputValidationPolicy(), base_ref="origin/main"
    )

    assert result.changed_paths == ("safe.txt",)


def test_validates_exact_requested_branch_not_head(repository: Path) -> None:
    _commit(repository, "safe.txt", "safe")
    _run(repository, "branch", "publish-me")
    _run(repository, "switch", "main")

    result = validate_repository_output(
        repository,
        OutputValidationPolicy(),
        base_ref="origin/main",
        head_ref="refs/heads/publish-me",
    )

    assert result.changed_paths == ("safe.txt",)
    assert result.head_ref == _run(repository, "rev-parse", "publish-me")


def test_repository_policy_is_loaded_from_trusted_base(repository: Path) -> None:
    _run(repository, "switch", "main")
    _commit(
        repository,
        ".forge-output-policy.yml",
        "version: 1\nprotected_paths:\n  - deploy/**\nmax_file_bytes: 4\n",
    )
    _run(repository, "push", "origin", "main")
    _run(repository, "switch", "forge/task-1")
    _run(repository, "rebase", "main")
    _commit(repository, "deploy/app.yml", "unsafe")

    with pytest.raises(OutputValidationError, match="protected path changed"):
        validate_repository_output(
            repository, OutputValidationPolicy(), base_ref="origin/main"
        )


def test_branch_cannot_weaken_trusted_policy(repository: Path) -> None:
    _run(repository, "switch", "main")
    _commit(
        repository,
        ".forge-output-policy.yml",
        "version: 1\nprotected_paths:\n  - deploy/**\n",
    )
    _run(repository, "push", "origin", "main")
    _run(repository, "switch", "forge/task-1")
    _run(repository, "rebase", "main")
    _commit(repository, ".forge-output-policy.yml", "version: 1\nprotected_paths: []\n")
    _commit(repository, "deploy/app.yml", "unsafe")

    with pytest.raises(OutputValidationError, match="protected path changed"):
        validate_repository_output(
            repository, OutputValidationPolicy(), base_ref="origin/main"
        )


def test_validator_receives_precomputed_immutable_context(repository: Path) -> None:
    _commit(repository, "src/example.py", "safe")

    class RecordingValidator:
        name = "recording"

        def validate(self, context) -> None:
            assert context.changed_entries[0].path == "src/example.py"
            with pytest.raises((AttributeError, TypeError)):
                context.changed_entries = ()

    validate_repository_output(
        repository, OutputValidationPolicy(), (RecordingValidator(),)
    )


def test_runs_additional_validator_after_safe_path_checks(repository: Path) -> None:
    _commit(repository, "src/example.py", "safe")

    class RecordingValidator:
        name = "secret_scanner"

        def __init__(self) -> None:
            self.paths: tuple[str, ...] = ()

        def validate(self, context) -> None:
            self.paths = context.changed_paths

    validator = RecordingValidator()
    validate_repository_output(repository, OutputValidationPolicy(), (validator,))

    assert validator.paths == ("src/example.py",)


def test_wraps_unexpected_validator_failure_as_fail_closed(repository: Path) -> None:
    _commit(repository, "src/example.py", "safe")

    class BrokenValidator:
        name = "broken"

        def validate(self, _context) -> None:
            raise RuntimeError("scanner unavailable")

    with pytest.raises(OutputValidationError, match="scanner unavailable"):
        validate_repository_output(repository, OutputValidationPolicy(), (BrokenValidator(),))
