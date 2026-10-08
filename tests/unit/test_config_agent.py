"""Global host-agent recursion configuration."""

import pytest
from pydantic import ValidationError

from forge.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        jira_base_url="https://jira.example.test",
        jira_api_token="dummy",
        jira_user_email="tester@example.test",
        github_token="dummy",
        llm_backend="vertex-ai",
        llm_model="claude-opus-4-8",
        google_cloud_project="offline-project",
        **overrides,
    )


def test_agent_recursion_limit_defaults_to_one_hundred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_RECURSION_LIMIT", raising=False)
    assert _settings().agent_recursion_limit == 100


def test_agent_recursion_limit_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RECURSION_LIMIT", "150")
    assert _settings().agent_recursion_limit == 150


@pytest.mark.parametrize("invalid", [0, -1])
def test_agent_recursion_limit_rejects_nonpositive_values(invalid: int) -> None:
    with pytest.raises(ValidationError, match="agent_recursion_limit"):
        _settings(agent_recursion_limit=invalid)


def test_vertex_output_strategy_defaults_to_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_STRUCTURED_OUTPUT_STRATEGY", raising=False)
    assert _settings().vertex_structured_output_strategy == "native"


def test_vertex_output_strategy_reads_tool_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_STRUCTURED_OUTPUT_STRATEGY", "tool")
    assert _settings().vertex_structured_output_strategy == "tool"


@pytest.mark.parametrize("invalid", ["", "auto", "TOOL", "invalid"])
def test_vertex_output_strategy_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("VERTEX_STRUCTURED_OUTPUT_STRATEGY", invalid)
    with pytest.raises(ValidationError, match="vertex_structured_output_strategy"):
        _settings()
