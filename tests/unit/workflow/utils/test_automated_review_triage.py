from types import SimpleNamespace

import pytest

from forge.workflow.utils.automated_review_triage import (
    is_bot_sender,
    parse_automated_review_decision,
    prepend_bot_prefix,
)


def test_bot_sender_uses_github_account_type() -> None:
    assert is_bot_sender({"sender": {"login": "anything", "type": "Bot"}})
    assert not is_bot_sender({"sender": {"login": "someone[bot]", "type": "User"}})


def test_parse_blocking_decision() -> None:
    decision = parse_automated_review_decision(
        '```json\n{"verdict":"blocking","blocking_feedback":"Fix auth","reason":"Required"}\n```'
    )
    assert decision.verdict == "blocking"
    assert decision.blocking_feedback == "Fix auth"


def test_parse_failure_is_uncertain() -> None:
    assert parse_automated_review_decision("Verdict: PASS").verdict == "uncertain"
    assert (
        parse_automated_review_decision(
            '{"verdict":"blocking","blocking_feedback":"","reason":"Missing"}'
        ).verdict
        == "uncertain"
    )


def test_prepend_bot_prefix_empty_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Fallback to settings with empty prefix
    mock_settings = SimpleNamespace(forge_bot_comment_prefix="")
    import forge.config

    monkeypatch.setattr(forge.config, "get_settings", lambda: mock_settings)

    # Empty prefix in settings, and prefix parameter is None/omitted
    assert prepend_bot_prefix("This is a comment", prefix=None) == "This is a comment"
    assert prepend_bot_prefix("This is a comment") == "This is a comment"

    # 2. Empty prefix via parameter override
    assert prepend_bot_prefix("This is a comment", prefix="") == "This is a comment"
    assert prepend_bot_prefix("This is a comment", prefix="   ") == "This is a comment"


def test_prepend_bot_prefix_normal_prefix() -> None:
    # Prefix not wrapped
    assert (
        prepend_bot_prefix("This is a comment", prefix="my-prefix")
        == "<!-- my-prefix -->\n\nThis is a comment"
    )
    # When comment body is empty
    assert prepend_bot_prefix("", prefix="my-prefix") == "<!-- my-prefix -->"


def test_prepend_bot_prefix_already_wrapped_prefix() -> None:
    # Prefix already wrapped with spaces
    assert (
        prepend_bot_prefix("This is a comment", prefix="<!-- my-prefix -->")
        == "<!-- my-prefix -->\n\nThis is a comment"
    )
    # Prefix already wrapped without internal spaces
    assert (
        prepend_bot_prefix("This is a comment", prefix="<!--my-prefix-->")
        == "<!--my-prefix-->\n\nThis is a comment"
    )


def test_prepend_bot_prefix_already_prepended_comment() -> None:
    # Comment already starts with the wrapped prefix (exact)
    comment = "<!-- my-prefix -->\n\nThis is a comment"
    assert prepend_bot_prefix(comment, prefix="my-prefix") == comment

    # Comment already starts with the wrapped prefix, but exact match of the prefix itself
    comment_only_prefix = "<!-- my-prefix -->"
    assert prepend_bot_prefix(comment_only_prefix, prefix="my-prefix") == comment_only_prefix

    # Comment already starts with wrapped prefix with leading/trailing whitespaces in comment
    comment_with_whitespace = "   \n  <!-- my-prefix -->\n\nThis is a comment"
    assert (
        prepend_bot_prefix(comment_with_whitespace, prefix="my-prefix") == comment_with_whitespace
    )


def test_prepend_bot_prefix_parameter_override(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = SimpleNamespace(forge_bot_comment_prefix="settings-prefix")
    import forge.config

    monkeypatch.setattr(forge.config, "get_settings", lambda: mock_settings)

    # When prefix parameter is explicitly passed, it should override settings-prefix
    assert (
        prepend_bot_prefix("This is a comment", prefix="param-prefix")
        == "<!-- param-prefix -->\n\nThis is a comment"
    )


def test_prepend_bot_prefix_settings_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = SimpleNamespace(forge_bot_comment_prefix="settings-prefix")
    import forge.config

    monkeypatch.setattr(forge.config, "get_settings", lambda: mock_settings)

    # Fallback when prefix is None or omitted
    assert (
        prepend_bot_prefix("This is a comment", prefix=None)
        == "<!-- settings-prefix -->\n\nThis is a comment"
    )
    assert (
        prepend_bot_prefix("This is a comment") == "<!-- settings-prefix -->\n\nThis is a comment"
    )
