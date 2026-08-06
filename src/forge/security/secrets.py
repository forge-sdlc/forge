"""Fail-closed secret detection for repository output and outbound text."""

from __future__ import annotations

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


class SecretScanError(RuntimeError):
    """A scan could not establish that output is safe to externalize."""


@dataclass(frozen=True)
class SecretFinding:
    """Redacted scanner result. The matched value is deliberately unavailable."""

    rule_id: str
    location: str
    line: int


@dataclass(frozen=True)
class _ScannedFinding:
    public: SecretFinding
    secret_hash: str


class SecretDetectedError(SecretScanError):
    """One or more secrets were found."""

    def __init__(self, findings: list[SecretFinding]):
        self.findings = findings
        locations = ", ".join(f"{f.location}:{f.line} ({f.rule_id})" for f in findings[:10])
        suffix = "" if len(findings) <= 10 else f" and {len(findings) - 10} more"
        super().__init__(f"Secret scan blocked externalization: {locations}{suffix}")


def _scan_files(files: list[tuple[Path, str]]) -> list[_ScannedFinding]:
    findings: list[_ScannedFinding] = []
    with default_settings():
        for path, display_name in files:
            secrets = SecretsCollection()
            secrets.scan_file(str(path))
            for detected in secrets.data.get(str(path), set()):
                findings.append(
                    _ScannedFinding(
                        public=SecretFinding(
                            rule_id=detected.type,
                            location=display_name,
                            line=detected.line_number,
                        ),
                        secret_hash=detected.secret_hash,
                    )
                )
    return findings


def _run_bounded(files: list[tuple[Path, str]], timeout_seconds: float) -> list[_ScannedFinding]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="secret-scan")
    future = pool.submit(_scan_files, files)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise SecretScanError(f"Secret scan timed out after {timeout_seconds:g}s") from exc
    except SecretScanError:
        raise
    except Exception as exc:
        raise SecretScanError(f"Secret scanner failed ({type(exc).__name__})") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def scan_text(text: str, *, location: str, timeout_seconds: float = 10) -> None:
    """Scan outbound text before it is posted. Raises with redacted details."""
    if not text:
        return
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt") as output:
            output.write(text)
            output.flush()
            findings = _run_bounded([(Path(output.name), location)], timeout_seconds)
    except SecretScanError:
        raise
    except Exception as exc:
        raise SecretScanError(f"Secret scanner failed ({type(exc).__name__})") from exc
    if findings:
        raise SecretDetectedError([finding.public for finding in findings])


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretScanError(f"Unable to prepare secret scan ({type(exc).__name__})") from exc
    return result.stdout


def _trusted_base(repo: Path) -> str:
    for candidate in ("origin/HEAD", "origin/main", "origin/master"):
        try:
            return _git(repo, "merge-base", "HEAD", candidate).strip()
        except SecretScanError:
            continue
    raise SecretScanError("Unable to resolve a trusted origin base for secret scanning")


def _baseline_findings(repo: Path, base: str) -> set[tuple[str, str]]:
    result = subprocess.run(
        ["git", "show", f"{base}:.secrets.baseline"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return set()
    try:
        baseline = json.loads(result.stdout)
        return {
            (filename, item["hashed_secret"])
            for filename, entries in baseline.get("results", {}).items()
            for item in entries
            if isinstance(filename, str)
            if isinstance(item.get("hashed_secret"), str)
        }
    except (TypeError, json.JSONDecodeError, KeyError) as exc:
        raise SecretScanError("Trusted .secrets.baseline is invalid") from exc


def scan_repository(repo: Path, *, timeout_seconds: float = 30) -> None:
    """Scan all changed and untracked output relative to the trusted origin base."""
    repo = repo.resolve()
    base = _trusted_base(repo)
    changed = set(_git(repo, "diff", "--name-only", "--diff-filter=ACMRT", "-z", base).split("\0"))
    changed.update(_git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    changed.discard("")

    files: list[tuple[Path, str]] = []
    for name in sorted(changed):
        path = repo / name
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SecretScanError(f"Unable to inspect output path {name!r}") from exc
        if not resolved.is_relative_to(repo) or path.is_symlink():
            raise SecretScanError(f"Unsafe output path cannot be scanned: {name!r}")
        if resolved.is_file():
            files.append((resolved, name))

    allowed = _baseline_findings(repo, base)
    findings = [
        finding
        for finding in _run_bounded(files, timeout_seconds)
        if (finding.public.location, finding.secret_hash) not in allowed
    ]
    if findings:
        raise SecretDetectedError([finding.public for finding in findings])
