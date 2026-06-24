"""Utilities for removing secrets from user-visible text."""

import re

_URL_CREDENTIALS_RE = re.compile(r"(https?://)([^/\s@]+)@")
_GITHUB_TOKEN_RE = re.compile(
    r"\b("
    r"gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]+"
    r")\b"
)


def redact_secrets(value: object) -> str:
    """Return text with credentials and access tokens redacted."""
    text = str(value)
    text = _URL_CREDENTIALS_RE.sub(r"\1[REDACTED]@", text)
    return _GITHUB_TOKEN_RE.sub("[REDACTED]", text)
