from types import SimpleNamespace

import pytest

from forge.integrations.github.comment_signature import (
    is_self_comment,
    prepend_bot_prefix,
    resolve_bot_login,
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


def test_is_self_comment_bot_suffix() -> None:
    # Usernames ending in [bot] are identified as self-comments if they match the bot login base
    assert is_self_comment("my-app[bot]", "Hello", "my-app", "some-prefix") is True
    assert is_self_comment("my-app[BOT]", "Hello", "my-app", "some-prefix") is True
    assert is_self_comment("forge-bot[bot]", "Some text", "forge-bot", None) is True


def test_is_self_comment_prefix_matching() -> None:
    # Configured prefix matching correctly identifies self-comments when the comment body starts with the prefix
    assert (
        is_self_comment(
            "forge-bot", "<!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )
    assert (
        is_self_comment(
            "forge-bot", "<!--my-prefix--> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )
    assert (
        is_self_comment("forge-bot", "my-prefix This is bot comment", "forge-bot", "my-prefix")
        is True
    )
    assert (
        is_self_comment(
            "FORGE-BOT", "<!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )
    # Check leading whitespace handling
    assert (
        is_self_comment(
            "forge-bot", "  \n <!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )


def test_is_self_comment_prefix_matching_returns_false_if_no_match() -> None:
    # Configured prefix matching returns False when the comment is from the bot login but the body does not start with the prefix
    assert is_self_comment("forge-bot", "This is human comment", "forge-bot", "my-prefix") is False
    assert (
        is_self_comment("forge-bot", "Some prefix-like text but not it", "forge-bot", "my-prefix")
        is False
    )


def test_is_self_comment_prefix_matching_returns_false_if_sender_mismatch() -> None:
    assert (
        is_self_comment(
            "other-user", "<!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is False
    )


def test_is_self_comment_legacy_fallback() -> None:
    # Legacy fallback (empty/unset prefix) matches exactly by username (case-insensitive)
    assert is_self_comment("forge-bot", "Hello", "forge-bot", None) is True
    assert is_self_comment("forge-bot", "Hello", "forge-bot", "") is True
    assert is_self_comment("forge-bot", "Hello", "forge-bot", "   ") is True
    assert is_self_comment("FORGE-bot", "Hello", "forge-bot", "") is True
    assert is_self_comment("other-user", "Hello", "forge-bot", None) is False


def test_is_self_comment_sc001_prefix_configured() -> None:
    # 1. Body starts with prefix (exact, wrapped with space, wrapped without space)
    assert (
        is_self_comment(
            "forge-bot", "<!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )
    assert (
        is_self_comment(
            "forge-bot", "<!--my-prefix--> This is bot comment", "forge-bot", "my-prefix"
        )
        is True
    )
    assert (
        is_self_comment("forge-bot", "my-prefix This is bot comment", "forge-bot", "my-prefix")
        is True
    )

    # 2. Body contains prefix but not at start
    assert (
        is_self_comment(
            "forge-bot",
            "This is bot comment but <!-- my-prefix --> is in middle",
            "forge-bot",
            "my-prefix",
        )
        is False
    )
    assert (
        is_self_comment(
            "forge-bot",
            "Some text, then my-prefix",
            "forge-bot",
            "my-prefix",
        )
        is False
    )

    # 3. Incorrect username with prefix (even if body starts with prefix, sender mismatch should return False)
    assert (
        is_self_comment(
            "other-user", "<!-- my-prefix --> This is bot comment", "forge-bot", "my-prefix"
        )
        is False
    )


def test_is_self_comment_sc002_prefix_empty_disabled() -> None:
    # 1. Matching username (should fallback to username match and return True)
    assert is_self_comment("forge-bot", "Hello", "forge-bot", None) is True
    assert is_self_comment("forge-bot", "Hello", "forge-bot", "") is True
    assert is_self_comment("forge-bot", "Hello", "forge-bot", "   ") is True
    assert is_self_comment("FORGE-bot", "Hello", "forge-bot", None) is True

    # 2. Different username (should fallback to username match and return False)
    assert is_self_comment("other-user", "Hello", "forge-bot", None) is False
    assert is_self_comment("other-user", "Hello", "forge-bot", "") is False
    assert is_self_comment("other-user", "Hello", "forge-bot", "   ") is False


def test_is_self_comment_sc003_prefix_configured_body_not_start_with_prefix() -> None:
    # Prefix configured, matching username, body does NOT start with prefix
    assert is_self_comment("forge-bot", "This is human comment", "forge-bot", "my-prefix") is False
    assert (
        is_self_comment("forge-bot", "Some prefix-like text but not it", "forge-bot", "my-prefix")
        is False
    )


def test_is_self_comment_sc004_sender_username_bot_suffix() -> None:
    # Sender username ending in [bot] (case-insensitive)
    assert is_self_comment("my-app[bot]", "Hello", "my-app", "some-prefix") is True
    assert is_self_comment("my-app[BOT]", "Hello", "my-app", "some-prefix") is True
    assert is_self_comment("forge-bot[bot]", "Some text", "forge-bot", None) is True
    assert is_self_comment("github-actions[bot]", "Any comment body", "github-actions", "") is True


async def test_resolve_bot_login_returns_authenticated_login() -> None:
    class FakeGitHubClient:
        async def get_authenticated_user(self) -> dict:
            return {"login": "forge-bot"}

    assert await resolve_bot_login(FakeGitHubClient()) == "forge-bot"


async def test_resolve_bot_login_returns_empty_string_when_login_missing() -> None:
    class FakeGitHubClient:
        async def get_authenticated_user(self) -> dict:
            return {}

    assert await resolve_bot_login(FakeGitHubClient()) == ""
