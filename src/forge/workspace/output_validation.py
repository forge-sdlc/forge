"""Fail-closed validation of agent-produced repository output."""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from forge.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)


class OutputValidationError(RuntimeError):
    """Raised when repository output is unsafe to publish."""


@dataclass(frozen=True)
class OutputValidationPolicy:
    """Policy applied immediately before an external Git write."""

    protected_paths: tuple[str, ...] = ()
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024
    reject_symlinks: bool = True


@dataclass
class OutputValidationContext:
    """Stable input shared by all output validators."""

    repo_path: Path
    base_ref: str
    head_ref: str = "HEAD"
    changed_paths: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


class OutputValidator(Protocol):
    """Extension point for additional gates such as secret scanners."""

    name: str

    def validate(self, context: OutputValidationContext) -> None: ...


class SafeRepositoryOutputValidator:
    """Reject dangerous paths, links, and unexpectedly large output."""

    name = "safe_repository_output"

    def __init__(self, policy: OutputValidationPolicy):
        self.policy = policy

    def validate(self, context: OutputValidationContext) -> None:
        entries = _changed_entries(context.repo_path, context.base_ref, context.head_ref)
        context.changed_paths = tuple(path for _, _, path in entries)
        violations: list[str] = []
        total_size = 0

        for status, mode, path in entries:
            if not _is_safe_relative_path(path):
                violations.append(f"unsafe path: {path!r}")
                continue
            if _is_protected(path, self.policy.protected_paths):
                violations.append(f"protected path changed: {path}")
            if status == "D":
                continue
            if self.policy.reject_symlinks and mode == "120000":
                violations.append(f"symbolic link output is not allowed: {path}")
                continue
            size = _blob_size(context.repo_path, context.head_ref, path)
            total_size += size
            if size > self.policy.max_file_bytes:
                violations.append(
                    f"file exceeds {self.policy.max_file_bytes} bytes: {path} ({size} bytes)"
                )

        if total_size > self.policy.max_total_bytes:
            violations.append(
                f"changed output exceeds {self.policy.max_total_bytes} bytes ({total_size} bytes)"
            )
        if violations:
            raise OutputValidationError(
                "Unsafe repository output; push blocked: " + "; ".join(violations)
            )


def validate_repository_output(
    repo_path: Path,
    policy: OutputValidationPolicy,
    validators: tuple[OutputValidator, ...] = (),
) -> OutputValidationContext:
    """Run every configured validator, failing closed on inspection errors."""

    base_ref = _default_base_ref(repo_path)
    context = OutputValidationContext(repo_path=repo_path, base_ref=base_ref)
    configured: tuple[OutputValidator, ...] = (SafeRepositoryOutputValidator(policy), *validators)
    for validator in configured:
        try:
            validator.validate(context)
        except OutputValidationError:
            raise
        except Exception as exc:
            raise OutputValidationError(
                f"Output validator {validator.name!r} failed; push blocked: {redact_secrets(exc)}"
            ) from exc
    logger.info("Repository output passed %d validation gate(s)", len(configured))
    return context


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise OutputValidationError(
            f"Unable to inspect repository output; push blocked: {redact_secrets(detail)}"
        )
    return result.stdout


def _default_base_ref(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    symbolic = result.stdout.strip()
    if not symbolic.startswith("refs/remotes/"):
        raise OutputValidationError("Remote default branch is unavailable; push blocked")
    remote_default = symbolic.removeprefix("refs/remotes/")
    return _git(repo_path, "merge-base", "HEAD", remote_default).strip()


def _changed_entries(repo_path: Path, base_ref: str, head_ref: str) -> list[tuple[str, str, str]]:
    output = _git(
        repo_path,
        "diff",
        "--raw",
        "-z",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
        base_ref,
        head_ref,
    )
    entries: list[tuple[str, str, str]] = []
    fields = output.split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    if len(fields) % 2:
        raise OutputValidationError("Malformed Git diff metadata; push blocked")
    for header, path in zip(fields[::2], fields[1::2], strict=True):
        metadata = header.split()
        if len(metadata) != 5:
            raise OutputValidationError("Malformed Git diff metadata; push blocked")
        old_mode, new_mode, _old_sha, _new_sha, status = metadata
        entries.append((status[0], old_mode if status[0] == "D" else new_mode, path))
    return entries


def _blob_size(repo_path: Path, ref: str, path: str) -> int:
    raw = _git(repo_path, "cat-file", "-s", f"{ref}:{path}").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise OutputValidationError(f"Invalid blob size for {path!r}; push blocked") from exc


def _is_safe_relative_path(path: str) -> bool:
    if not path or "\x00" in path or "\n" in path or "\r" in path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts and not path.startswith("-")


def _is_protected(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.removeprefix("./")
    return any(
        normalized == pattern.rstrip("/")
        or normalized.startswith(pattern.rstrip("/") + "/")
        or fnmatch.fnmatchcase(normalized, pattern)
        for pattern in patterns
    )
