"""Tests for passing resolved model backend configuration to containers."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from forge.sandbox.runner import ContainerConfig, ContainerRunner


def _settings(backend: str) -> MagicMock:
    settings = MagicMock(
        llm_backend=backend,
        container_model="gemini-3.5-flash",
        llm_max_tokens=16384,
        git_user_name="Forge",
        git_user_email="forge@example.com",
        container_command_timeout=1200,
        langfuse_enabled=False,
        container_langchain_verbose=False,
        log_level="INFO",
        google_cloud_project="project",
        google_cloud_location="global",
        openai_base_url="https://gateway.example/v1",
    )
    settings.google_api_key = SecretStr("google-key")
    settings.anthropic_api_key = SecretStr("anthropic-key")
    settings.resolve_openai_api_key.return_value = "gateway-key"
    return settings


@pytest.mark.parametrize(
    ("backend", "expected", "absent"),
    [
        ("google-genai", {"GOOGLE_API_KEY": "google-key"}, "ANTHROPIC_API_KEY"),
        ("anthropic", {"ANTHROPIC_API_KEY": "anthropic-key"}, "GOOGLE_API_KEY"),
        (
            "vertex-ai",
            {"GOOGLE_CLOUD_PROJECT": "project", "GOOGLE_CLOUD_LOCATION": "global"},
            "GOOGLE_API_KEY",
        ),
        (
            "openai-compatible",
            {
                "OPENAI_BASE_URL": "https://gateway.example/v1",
                "OPENAI_API_KEY": "gateway-key",
            },
            "ANTHROPIC_API_KEY",
        ),
    ],
)
def test_build_env_vars_passes_only_selected_backend_credentials(
    backend: str, expected: dict[str, str], absent: str
):
    runner = ContainerRunner.__new__(ContainerRunner)
    runner.settings = _settings(backend)

    with patch("forge.sandbox.runner.load_prompt", return_value="prompt"):
        env = runner._build_env_vars(ContainerConfig())

    assert env["LLM_BACKEND"] == backend
    assert env.items() >= expected.items()
    assert absent not in env
    assert env["CONTAINER_COMMAND_TIMEOUT"] == "1200"


def test_command_timeout_clamped_to_container_lifetime():
    """A shorter ad-hoc container lifetime caps the per-command timeout.

    podman kills the container at config.timeout_seconds, so exporting a larger
    per-command budget would surface as a confusing 'container killed' instead of
    a clean 'command timed out'.
    """
    runner = ContainerRunner.__new__(ContainerRunner)
    runner.settings = _settings("anthropic")  # container_command_timeout=1200

    with patch("forge.sandbox.runner.load_prompt", return_value="prompt"):
        env = runner._build_env_vars(ContainerConfig(timeout_seconds=300))

    assert env["CONTAINER_COMMAND_TIMEOUT"] == "300"
