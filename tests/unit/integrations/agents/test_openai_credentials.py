"""Compatible endpoints receive only the credential selected for that connection."""

from unittest.mock import patch

import pytest

from forge.config import Settings
from forge.integrations.agents.agent import ForgeAgent
from forge.sandbox.runner import ContainerConfig, ContainerRunner


@pytest.mark.parametrize(
    ("mode", "resolved", "expected"),
    [
        ("named_anonymous", True, "not-required"),
        ("named_authenticated", True, "endpoint-key"),
        ("legacy", False, "legacy-key"),
        ("legacy", True, "legacy-key"),
        ("legacy_anonymous", False, "not-required"),
    ],
)
def test_host_and_container_credentials_stay_with_their_connection(
    monkeypatch, mode: str, resolved: bool, expected: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "" if mode == "legacy_anonymous" else "legacy-key")
    monkeypatch.setenv("ENDPOINT_API_KEY", "endpoint-key")
    connections = {}
    default = {}
    if mode.startswith("named_"):
        connection = {
            "backend": "openai-compatible",
            "base_url": "https://gateway.example/v1",
            "capabilities": ["tools", "structured_output"],
        }
        if mode == "named_authenticated":
            connection["api_key_env"] = "ENDPOINT_API_KEY"
        connections = {"gateway": connection}
        default = {"connection": "gateway", "model": "custom-model"}
    settings = Settings(
        _env_file=None,
        jira_base_url="https://test.atlassian.net",
        jira_api_token="test",
        jira_user_email="test@example.com",
        github_token="test",
        llm_backend="openai-compatible",
        llm_model="custom-model",
        openai_base_url="https://gateway.example/v1",
        model_connections=connections,
        model_default=default,
        model_policy={},
        langfuse_enabled=False,
    )
    target = settings.model_policy_resolver().resolve("generate_prd") if resolved else None
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = settings
    model = agent._create_model(model_target=target)
    assert model.openai_api_key.get_secret_value() == expected

    runner = ContainerRunner.__new__(ContainerRunner)
    runner.settings = settings
    with patch("forge.sandbox.runner.load_prompt", return_value="prompt"):
        env = runner._build_env_vars(ContainerConfig(), model_target=target)
    assert env["OPENAI_API_KEY"] == expected
    assert env["OPENAI_BASE_URL"] == "https://gateway.example/v1"
