"""Tests for bot signature/comment prefix configuration."""

from typing import Any

import pytest

from forge.config import Settings


@pytest.fixture(autouse=True)
def clear_bot_prefix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_BOT_COMMENT_PREFIX", raising=False)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("CONTAINER_LLM_MODEL", raising=False)
    monkeypatch.delenv("MODEL_CONNECTIONS", raising=False)
    monkeypatch.delenv("MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("MODEL_POLICY", raising=False)


def make_settings(**kwargs: Any) -> Settings:
    # Use dummy values for required settings so that Settings can instantiate
    kwargs.setdefault("jira_base_url", "https://test.atlassian.net")
    kwargs.setdefault("jira_api_token", "test-token")
    kwargs.setdefault("jira_user_email", "test@example.com")
    kwargs.setdefault("github_token", "test-github-token")
    kwargs.setdefault("llm_backend", "vertex-ai")
    kwargs.setdefault("llm_model", "gemini-3.5-flash")
    kwargs.setdefault("google_cloud_project", "test-project")
    return Settings(**kwargs)


class TestBotSignatureConfig:
    def test_default_bot_comment_prefix_is_empty(self) -> None:
        settings = make_settings()
        assert settings.forge_bot_comment_prefix == ""

    def test_bot_comment_prefix_can_be_set_via_init(self) -> None:
        settings = make_settings(forge_bot_comment_prefix="[BOT-SIG] ")
        assert settings.forge_bot_comment_prefix == "[BOT-SIG] "

    def test_bot_comment_prefix_is_loaded_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FORGE_BOT_COMMENT_PREFIX", "[FORGE] ")
        settings = make_settings()
        assert settings.forge_bot_comment_prefix == "[FORGE] "
