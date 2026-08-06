"""Fail-closed validation of agent-produced repository output."""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import yaml

from forge.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)


class OutputValidationError(RuntimeError):
    """Raised when repository output is unsafe to publish."""


@dataclass(frozen=True)
class OutputValidationPolicy:
    """Policy applied immediately before an external Git write."""

    protected_paths: tuple[str, ...] = ()
    max_file_bytes: int | None = None
    max_total_bytes: int | None = None
    reject_symlinks: bool = False


@dataclass(frozen=True)
class ChangedEntry:
    """Immutable Git-tree metadata for one changed path."""

    status: str
    mode: str
    path: str
    size_bytes: int | None


@dataclass(frozen=True)
class OutputValidationContext:
    """Stable input shared by all output validators."""

    repo_path: Path
    base_ref: str
    head_ref: str
    changed_entries: tuple[ChangedEntry, ...]
    metadata: tuple[tuple[str, object], ...] = ()

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.changed_entries)


@dataclass(frozen=True)
class OutputValidationResult:
    """Successful, structured decision at the repository publication boundary."""

    base_ref: str
    head_ref: str
    changed_paths: tuple[str, ...]
    validators: tuple[str, ...]


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
        violations: list[str] = []
        total_size = 0

        for entry in context.changed_entries:
            status, mode, path = entry.status, entry.mode, entry.path
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
            if entry.size_bytes is None:
                raise OutputValidationError(f"Missing blob size for {path!r}; push blocked")
            size = entry.size_bytes
            total_size += size
            if self.policy.max_file_bytes is not None and size > self.policy.max_file_bytes:
                violations.append(
                    f"file exceeds {self.policy.max_file_bytes} bytes: {path} ({size} bytes)"
                )

        if self.policy.max_total_bytes is not None and total_size > self.policy.max_total_bytes:
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
    *,
    base_ref: str | None = None,
    head_ref: str = "HEAD",
) -> OutputValidationResult:
    """Run every configured validator, failing closed on inspection errors."""

    resolved_head = _resolve_ref(repo_path, head_ref, "output branch")
    resolved_base = _resolve_base_ref(repo_path, resolved_head, base_ref)
    effective_policy = _load_trusted_policy(repo_path, resolved_base, policy)
    entries = tuple(_changed_entries(repo_path, resolved_base, resolved_head))
    context = OutputValidationContext(
        repo_path=repo_path,
        base_ref=resolved_base,
        head_ref=resolved_head,
        changed_entries=entries,
    )
    configured: tuple[OutputValidator, ...] = (
        SafeRepositoryOutputValidator(effective_policy),
        *validators,
    )
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
    return OutputValidationResult(
        base_ref=context.base_ref,
        head_ref=context.head_ref,
        changed_paths=context.changed_paths,
        validators=tuple(validator.name for validator in configured),
    )


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


def _resolve_ref(repo_path: Path, ref: str, label: str) -> str:
    try:
        return _git(repo_path, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except OutputValidationError as exc:
        raise OutputValidationError(f"Trusted {label} {ref!r} is unavailable; push blocked") from exc


def _resolve_base_ref(repo_path: Path, head_ref: str, configured_ref: str | None) -> str:
    if configured_ref:
        upstream = configured_ref
    else:
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
        upstream = symbolic.removeprefix("refs/remotes/")
    _resolve_ref(repo_path, upstream, "base ref")
    return _git(repo_path, "merge-base", head_ref, upstream).strip()


def _load_trusted_policy(
    repo_path: Path, base_ref: str, operator_policy: OutputValidationPolicy
) -> OutputValidationPolicy:
    """Load repository constraints from the trusted base; constraints only tighten."""
    policy_path = ".forge-output-policy.yml"
    result = subprocess.run(
        ["git", "show", f"{base_ref}:{policy_path}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return operator_policy
    try:
        raw = yaml.safe_load(result.stdout) or {}
        if not isinstance(raw, dict) or raw.get("version", 1) != 1:
            raise ValueError("policy must be a version 1 mapping")
        protected = raw.get("protected_paths", [])
        if not isinstance(protected, list) or not all(isinstance(item, str) for item in protected):
            raise ValueError("protected_paths must be a list of strings")
        max_file = _optional_positive_int(raw.get("max_file_bytes"), "max_file_bytes")
        max_total = _optional_positive_int(raw.get("max_total_bytes"), "max_total_bytes")
        reject_symlinks = raw.get("reject_symlinks", operator_policy.reject_symlinks)
        if not isinstance(reject_symlinks, bool):
            raise ValueError("reject_symlinks must be boolean")
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise OutputValidationError(f"Invalid trusted {policy_path}; push blocked: {exc}") from exc
    return OutputValidationPolicy(
        protected_paths=tuple(
            dict.fromkeys((*operator_policy.protected_paths, policy_path, *protected))
        ),
        max_file_bytes=_stricter_limit(operator_policy.max_file_bytes, max_file),
        max_total_bytes=_stricter_limit(operator_policy.max_total_bytes, max_total),
        reject_symlinks=operator_policy.reject_symlinks or reject_symlinks,
    )


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _stricter_limit(operator_limit: int | None, repository_limit: int | None) -> int | None:
    limits = [limit for limit in (operator_limit, repository_limit) if limit is not None]
    return min(limits) if limits else None


def _changed_entries(repo_path: Path, base_ref: str, head_ref: str) -> list[ChangedEntry]:
    output = _git(
        repo_path, "diff", "--raw", "--no-renames", "--diff-filter=ACDMRTUXB", base_ref, head_ref
    )
    entries: list[ChangedEntry] = []
    for line in output.splitlines():
        header, separator, path = line.partition("\t")
        if not separator:
            raise OutputValidationError("Malformed Git diff metadata; push blocked")
        fields = header.split()
        if len(fields) != 5:
            raise OutputValidationError("Malformed Git diff metadata; push blocked")
        old_mode, new_mode, _old_sha, _new_sha, status = fields
        kind = status[0]
        entries.append(
            ChangedEntry(
                status=kind,
                mode=old_mode if kind == "D" else new_mode,
                path=path,
                size_bytes=None if kind == "D" else _blob_size(repo_path, head_ref, path),
            )
        )
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
