"""Tests for secret redaction utilities."""

from forge.utils.redaction import redact_secrets


def test_redacts_github_token_in_authenticated_url():
    token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
    text = (
        f"https://x-access-token:{token}@github.com/org/repo.git"
    )

    redacted = redact_secrets(text)

    assert "ghp_" not in redacted
    assert redacted == "https://[REDACTED]@github.com/org/repo.git"


def test_redacts_standalone_github_token():
    text = "failed with token " + "github" + "_pat_abcdefghijklmnopqrstuvwxyz_1234567890"

    redacted = redact_secrets(text)

    assert "github_pat_" not in redacted
    assert "[REDACTED]" in redacted
