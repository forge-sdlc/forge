"""Tests for the trusted secret-scanning boundary."""

import json
import subprocess
import time
from pathlib import Path

import pytest
from detect_secrets.core.potential_secret import PotentialSecret

from forge.security.secrets import (
    SecretDetectedError,
    SecretScanError,
    scan_repository,
    scan_text,
)

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("safe\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_scan_text_blocks_provider_key_without_disclosing_value() -> None:
    with pytest.raises(SecretDetectedError) as caught:
        scan_text(f"aws_access_key_id = {AWS_KEY}", location="PR body")

    assert caught.value.findings[0].rule_id == "AWS Access Key"
    assert caught.value.findings[0].location == "PR body"
    assert AWS_KEY not in str(caught.value)
    assert AWS_KEY not in repr(caught.value.findings)


def test_scan_text_allows_ordinary_content() -> None:
    scan_text("Fix request parsing and add regression coverage.", location="comment")


def test_scan_text_blocks_generic_high_entropy_token() -> None:
    token = "v8N2qL7mR4xP9cT6zW3kJ5hF1sD0aB7u"
    with pytest.raises(SecretDetectedError) as caught:
        scan_text(f'api_token = "{token}"', location="artifact")

    assert token not in str(caught.value)


def test_scan_repository_covers_untracked_files(repository: Path) -> None:
    (repository / "generated.env").write_text(f"aws_access_key_id={AWS_KEY}\n")

    with pytest.raises(SecretDetectedError) as caught:
        scan_repository(repository)

    assert caught.value.findings[0].location == "generated.env"
    assert AWS_KEY not in str(caught.value)


def test_scan_repository_covers_committed_and_unstaged_changes(repository: Path) -> None:
    output = repository / "output.txt"
    output.write_text(f"aws_access_key_id={AWS_KEY}\n")
    _git(repository, "add", "output.txt")
    _git(repository, "commit", "-m", "agent output")
    (repository / "README.md").write_text("safe unstaged change\n")

    with pytest.raises(SecretDetectedError):
        scan_repository(repository)


def test_trusted_base_baseline_allows_known_finding(repository: Path) -> None:
    tracked = repository / "example.txt"
    tracked.write_text(f"aws_access_key_id={AWS_KEY}\n")
    baseline = {
        "version": "1.5.0",
        "results": {
            "example.txt": [
                {
                    "type": "AWS Access Key",
                    "filename": "example.txt",
                    "hashed_secret": PotentialSecret.hash_secret(AWS_KEY),
                }
            ]
        },
    }
    (repository / ".secrets.baseline").write_text(json.dumps(baseline))
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "trusted baseline")
    _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    tracked.write_text(f"note=safe\naws_access_key_id={AWS_KEY}\n")

    scan_repository(repository)


def test_trusted_baseline_does_not_allow_secret_copied_to_another_file(
    repository: Path,
) -> None:
    tracked = repository / "example.txt"
    tracked.write_text(f"aws_access_key_id={AWS_KEY}\n")
    baseline = {
        "version": "1.5.0",
        "results": {
            "example.txt": [
                {
                    "type": "AWS Access Key",
                    "filename": "example.txt",
                    "hashed_secret": PotentialSecret.hash_secret(AWS_KEY),
                }
            ]
        },
    }
    (repository / ".secrets.baseline").write_text(json.dumps(baseline))
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "trusted baseline")
    _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / "copied.txt").write_text(f"aws_access_key_id={AWS_KEY}\n")

    with pytest.raises(SecretDetectedError) as caught:
        scan_repository(repository)

    assert caught.value.findings[0].location == "copied.txt"


def test_binary_file_does_not_crash_scanner(repository: Path) -> None:
    (repository / "image.bin").write_bytes(b"\x00\xff\x10\x80")
    scan_repository(repository)


def test_symlink_output_fails_closed(repository: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("safe")
    (repository / "output-link").symlink_to(target)

    with pytest.raises(SecretScanError, match="Unsafe output path"):
        scan_repository(repository)


def test_missing_trusted_base_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("safe")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    with pytest.raises(SecretScanError, match="trusted origin base"):
        scan_repository(repo)


def test_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_scan(_files: object) -> list[object]:
        time.sleep(0.05)
        return []

    monkeypatch.setattr("forge.security.secrets._scan_files", slow_scan)
    with pytest.raises(SecretScanError, match="timed out"):
        scan_text("safe", location="comment", timeout_seconds=0.001)
