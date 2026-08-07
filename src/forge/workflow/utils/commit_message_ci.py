"""Detect CI failures caused by commit-message formatting rules."""

from __future__ import annotations

import re
from typing import Any

# Patterns commonly emitted by check-commits / commitlint-style gates.
_COMMIT_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"title is longer than \d+ characters", re.I),
    re.compile(r"title lacks a lowercase topic prefix", re.I),
    re.compile(r"signed-off-by:.+trailer is missing", re.I),
    re.compile(r"missing.*signed-off-by", re.I),
    re.compile(r"commit(?:\s+message)?(?:\s+format)?(?:\s+validation)?\s+fail", re.I),
    re.compile(r"check-commits", re.I),
    re.compile(r"commitlint", re.I),
    re.compile(r"conventional commits?", re.I),
    re.compile(r"\bgit-commits\b", re.I),
    re.compile(r"subject must( not)? be", re.I),
)

_COMMIT_CHECK_NAME_HINTS = (
    "check-commits",
    "git-commits",
    "commitlint",
    "commit-message",
    "commit message",
    "conventional-commit",
)


def _texts_from_check(check: dict[str, Any]) -> list[str]:
    texts = [str(check.get("name") or "")]
    output = check.get("output") or {}
    if isinstance(output, dict):
        for key in ("title", "summary", "text"):
            value = output.get(key)
            if value:
                texts.append(str(value))
    for key in ("error", "message", "details"):
        value = check.get(key)
        if value:
            texts.append(str(value))
    return texts


def is_commit_message_formatting_failure(failed_checks: list[dict[str, Any]]) -> bool:
    """Return True when every failed check looks like commit-message validation.

    Empty input is False. Mixed code + commit-message failures return False so
    normal code-fix retries still run.
    """
    if not failed_checks:
        return False

    for check in failed_checks:
        texts = _texts_from_check(check)
        blob = "\n".join(texts)
        name = str(check.get("name") or "").lower()
        name_hint = any(hint in name for hint in _COMMIT_CHECK_NAME_HINTS)
        pattern_hit = any(pat.search(blob) for pat in _COMMIT_MESSAGE_PATTERNS)
        if not (name_hint or pattern_hit):
            return False
    return True


def commit_message_failure_summary(failed_checks: list[dict[str, Any]]) -> str:
    """Build a short operator-facing summary of commit-message CI failures."""
    snippets: list[str] = []
    for check in failed_checks:
        name = check.get("name") or "commit check"
        output = check.get("output") or {}
        detail = ""
        if isinstance(output, dict):
            detail = str(output.get("summary") or output.get("title") or output.get("text") or "")
        detail = detail.strip().splitlines()[0] if detail.strip() else "commit message formatting"
        snippets.append(f"{name}: {detail[:200]}")
    joined = "; ".join(snippets) if snippets else "commit message formatting"
    return (
        "CI failed due to commit message format — please amend the commit message "
        f"to match repository conventions ({joined})"
    )
