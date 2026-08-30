"""Tests for PRD approval configuration settings."""

import pytest

from forge.config import Settings


@pytest.fixture(autouse=True)
def clear_prd_proposal_env(monkeypatch):
    monkeypatch.delenv("PRD_PROPOSALS_REPO", raising=False)
    monkeypatch.delenv("PRD_PROPOSALS_PATH", raising=False)
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("CONTAINER_LLM_MODEL", raising=False)
    monkeypatch.delenv("MODEL_CONNECTIONS", raising=False)
    monkeypatch.delenv("MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("MODEL_POLICY", raising=False)


def make_settings(**kwargs) -> Settings:
    kwargs.setdefault("llm_backend", "vertex-ai")
    kwargs.setdefault("llm_model", "gemini-3.5-flash")
    kwargs.setdefault("google_cloud_project", "test-project")
    return Settings(_env_file=None, **kwargs)


class TestPrdApprovalConfig:
    def test_default_proposals_repo_is_empty(self) -> None:
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_repo == ""

    def test_default_proposals_path(self) -> None:
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
        )
        assert settings.prd_proposals_path == ""

    def test_proposals_repo_can_be_set_as_global_fallback(self) -> None:
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            anthropic_api_key="test",
            prd_proposals_repo="org/proposals",
        )
        assert settings.prd_proposals_repo == "org/proposals"


class TestLlmConfig:
    def test_provider_neutral_environment_does_not_require_legacy_vars(self, monkeypatch):
        monkeypatch.setenv(
            "MODEL_CONNECTIONS",
            '{"vertex":{"backend":"vertex-ai","project":"policy-project",'
            '"location":"global","allowed_models":["gemini-3.5-flash"],'
            '"capabilities":["tools"]}}',
        )
        monkeypatch.setenv(
            "MODEL_DEFAULT",
            '{"connection":"vertex","model":"gemini-3.5-flash"}',
        )

        settings = Settings(
            _env_file=None,
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
        )

        assert settings.llm_backend == "vertex-ai"
        assert settings.llm_model == "gemini-3.5-flash"
        assert settings.google_cloud_project == "policy-project"

    def test_complete_model_policy_configuration_derives_legacy_runtime_fields(self):
        settings = Settings(
            _env_file=None,
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            model_connections={
                "vertex-global": {
                    "backend": "vertex-ai",
                    "project": "policy-project",
                    "location": "global",
                    "allowed_models": ["gemini-3.5-flash", "claude-sonnet-5"],
                    "capabilities": ["tools"],
                }
            },
            model_default={
                "connection": "vertex-global",
                "model": "gemini-3.5-flash",
            },
        )

        assert settings.llm_backend == "vertex-ai"
        assert settings.llm_model == "gemini-3.5-flash"
        assert settings.google_cloud_project == "policy-project"
        assert settings.google_cloud_location == "global"

    def test_model_connections_without_default_or_legacy_config_is_rejected(self):
        with pytest.raises(ValueError, match="MODEL_DEFAULT is required"):
            Settings(
                _env_file=None,
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                model_connections={
                    "vertex": {
                        "backend": "vertex-ai",
                        "project": "policy-project",
                    }
                },
            )

    def test_legacy_model_configuration_builds_effective_default(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
        )

        assert settings.effective_model_connections["default"]["backend"] == "vertex-ai"
        assert settings.effective_model_connections["default"]["allowed_models"] == [
            "gemini-3.5-flash"
        ]
        assert settings.effective_model_connections["default"]["capabilities"] == ["tools"]
        assert settings.effective_model_default == {
            "connection": "default",
            "model": "gemini-3.5-flash",
        }
        assert settings.has_explicit_model_policy is False

    def test_new_model_configuration_is_detected_explicitly(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            model_policy={"generate_prd": {"connection": "default", "model": "gemini-3.5-flash"}},
        )

        assert settings.has_explicit_model_policy is True

    def test_named_connection_backend_credentials_are_validated(self):
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY.*anthropic model connection"):
            make_settings(
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                model_connections={
                    "vertex": {
                        "backend": "vertex-ai",
                        "project": "test-project",
                    },
                    "anthropic": {
                        "backend": "anthropic",
                        "allowed_models": ["claude-sonnet"],
                    },
                },
                model_default={"connection": "vertex", "model": "gemini-pro"},
            )

    def test_backend_is_required(self):
        with pytest.raises(ValueError, match="llm_backend"):
            Settings(
                _env_file=None,
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                llm_model="gemini-3.5-flash",
            )

    def test_explicit_vertex_gemini_configuration(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
        )

        assert settings.llm_backend == "vertex-ai"
        assert settings.llm_model == "gemini-3.5-flash"
        assert Settings.detect_model_provider(settings.llm_model) == "google"

    @pytest.mark.parametrize(
        ("backend", "model", "credential", "message"),
        [
            ("vertex-ai", "gemini-3.5-flash", {}, "GOOGLE_CLOUD_PROJECT is required"),
            ("google-genai", "gemini-3.5-flash", {}, "GOOGLE_API_KEY is required"),
            ("anthropic", "claude-sonnet-4-6", {}, "ANTHROPIC_API_KEY is required"),
        ],
    )
    def test_backend_credentials_are_validated_at_startup(
        self, backend, model, credential, message
    ):
        with pytest.raises(ValueError, match=message):
            Settings(
                _env_file=None,
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                llm_backend=backend,
                llm_model=model,
                **credential,
            )

    def test_container_model_must_match_backend(self):
        with pytest.raises(ValueError, match="not supported by google-genai"):
            make_settings(
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                llm_backend="google-genai",
                llm_model="gemini-3.5-flash",
                container_llm_model="claude-sonnet-4-6",
                google_api_key="google-key",
            )

    def test_google_api_key_is_provider_specific(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            google_api_key="google-key",
        )

        assert settings.google_api_key.get_secret_value() == "google-key"
        assert settings.llm_backend == "vertex-ai"

    def test_anthropic_configuration_is_explicit(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            llm_backend="anthropic",
            llm_model="claude-sonnet-4-6",
            anthropic_api_key="anthropic-key",
        )

        assert settings.llm_backend == "anthropic"
        assert settings.llm_model == "claude-sonnet-4-6"
        assert settings.anthropic_api_key.get_secret_value() == "anthropic-key"

    def test_openai_compatible_named_connection_resolves_its_credential(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "gateway-secret")
        settings = Settings(
            _env_file=None,
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            model_connections={
                "gateway": {
                    "backend": "openai-compatible",
                    "base_url": "https://gateway.example/v1",
                    "api_key_env": "GATEWAY_API_KEY",
                    "capabilities": ["tools"],
                }
            },
            model_default={"connection": "gateway", "model": "custom-model"},
        )

        assert settings.llm_backend == "openai-compatible"
        assert settings.openai_base_url == "https://gateway.example/v1"
        assert settings.resolve_openai_api_key("GATEWAY_API_KEY") == "gateway-secret"

    def test_openai_compatible_missing_referenced_credential_fails(self):
        with pytest.raises(ValueError, match="MISSING_GATEWAY_KEY.*not set"):
            Settings(
                _env_file=None,
                jira_base_url="https://test.atlassian.net",
                jira_api_token="test",
                jira_user_email="test@example.com",
                github_token="test",
                model_connections={
                    "gateway": {
                        "backend": "openai-compatible",
                        "base_url": "https://gateway.example/v1",
                        "api_key_env": "MISSING_GATEWAY_KEY",
                    }
                },
                model_default={"connection": "gateway", "model": "custom-model"},
            )

    def test_google_cloud_settings_use_provider_native_names(self):
        settings = make_settings(
            jira_base_url="https://test.atlassian.net",
            jira_api_token="test",
            jira_user_email="test@example.com",
            github_token="test",
            llm_backend="vertex-ai",
            google_cloud_project="google-project",
            google_cloud_location="europe-west1",
        )

        assert settings.google_cloud_project == "google-project"
        assert settings.google_cloud_location == "europe-west1"
