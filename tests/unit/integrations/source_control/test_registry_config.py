"""Tests for repos.yaml config loading and validation."""

import pytest

from forge.integrations.source_control.contracts import Provider
from forge.integrations.source_control.errors import ProviderConfigError
from forge.integrations.source_control.registry import load_registry


def _write_config(tmp_path, content: str):
    path = tmp_path / "repos.yaml"
    path.write_text(content)
    return path


def test_missing_config_file_loads_an_empty_registry(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "does-not-exist.yaml", settings=mock_settings)

    assert registry.get_repository("anything") is None
    assert registry.get_connection("anything") is None


def test_loads_a_connection_and_repository(tmp_path, mock_settings, monkeypatch):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    base_url: https://gitlab.acme.example.com
    credential_env: ACME_GITLAB_TOKEN
    webhook_secret_env: ACME_GITLAB_WEBHOOK_SECRET
    allowed_namespaces: ["platform/payments"]

repositories:
  payments-api:
    provider: gitlab
    connection: acme-gitlab
    namespace: platform/payments
    default_branch: main
    change_request_mode: direct
""",
    )

    registry = load_registry(config_path=config, settings=mock_settings)

    connection = registry.get_connection("acme-gitlab")
    assert connection is not None
    assert connection.provider is Provider.GITLAB
    assert connection.credential_env == "ACME_GITLAB_TOKEN"

    repo = registry.get_repository("payments-api")
    assert repo is not None
    assert repo.namespace == "platform/payments"
    assert repo.change_request_mode == "direct"


def test_rejects_unknown_provider(tmp_path, mock_settings):
    config = _write_config(
        tmp_path,
        """
connections:
  bad:
    provider: bitbucket
    credential_env: SOME_TOKEN
""",
    )

    with pytest.raises(ProviderConfigError, match="unknown provider"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_explicit_connection_named_like_the_implicit_github_connection(
    tmp_path, mock_settings, monkeypatch
):
    """An explicit connection named 'github-default' would collide in the
    adapter cache with the implicit zero-config GitHub connection, silently
    mixing up which credential/host a resolution uses."""
    monkeypatch.setenv("SOME_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  github-default:
    provider: github
    credential_env: SOME_TOKEN
""",
    )

    with pytest.raises(ProviderConfigError, match="reserved implicit connection name"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_repository_with_unknown_connection(tmp_path, mock_settings, monkeypatch):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN

repositories:
  payments-api:
    provider: gitlab
    connection: does-not-exist
    namespace: platform/payments
""",
    )

    with pytest.raises(ProviderConfigError, match="unknown connection"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_connection_with_missing_credential_env_var(tmp_path, mock_settings, monkeypatch):
    monkeypatch.delenv("UNSET_TOKEN_VAR", raising=False)
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: UNSET_TOKEN_VAR
""",
    )

    with pytest.raises(ProviderConfigError, match="UNSET_TOKEN_VAR"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_repository_namespace_excluded_by_allowed_namespaces(
    tmp_path, mock_settings, monkeypatch
):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN
    allowed_namespaces: ["platform/other"]

repositories:
  payments-api:
    provider: gitlab
    connection: acme-gitlab
    namespace: platform/payments
""",
    )

    with pytest.raises(ProviderConfigError, match="allowed_namespaces"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_connection_with_string_allowed_namespaces(tmp_path, mock_settings, monkeypatch):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN
    allowed_namespaces: "platform/payments"
""",
    )

    with pytest.raises(ProviderConfigError, match="allowed_namespaces.*list"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_connection_with_non_string_allowed_namespaces_elements(
    tmp_path, mock_settings, monkeypatch
):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN
    allowed_namespaces: [123, 456]
""",
    )

    with pytest.raises(ProviderConfigError, match="allowed_namespaces.*list of strings"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_repository_provider_mismatched_with_connection(
    tmp_path, mock_settings, monkeypatch
):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN

repositories:
  payments-api:
    provider: github
    connection: acme-gitlab
    namespace: platform/payments
""",
    )

    with pytest.raises(ProviderConfigError, match="provider"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_two_repositories_colliding_on_provider_and_namespace(
    tmp_path, mock_settings, monkeypatch
):
    monkeypatch.setenv("ACME_GITLAB_TOKEN", "secret")
    config = _write_config(
        tmp_path,
        """
connections:
  acme-gitlab:
    provider: gitlab
    credential_env: ACME_GITLAB_TOKEN

repositories:
  payments-api:
    provider: gitlab
    connection: acme-gitlab
    namespace: platform/payments
  payments-api-dup:
    provider: gitlab
    connection: acme-gitlab
    namespace: platform/payments
""",
    )

    with pytest.raises(ProviderConfigError, match="namespace"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_non_mapping_top_level_document(tmp_path, mock_settings):
    config = _write_config(tmp_path, "- just\n- a\n- list\n")

    with pytest.raises(ProviderConfigError, match="mapping"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_non_mapping_connections_section(tmp_path, mock_settings):
    config = _write_config(tmp_path, "connections: [not, a, mapping]\n")

    with pytest.raises(ProviderConfigError, match="'connections'"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_non_mapping_repositories_section(tmp_path, mock_settings):
    config = _write_config(tmp_path, "repositories: [not, a, mapping]\n")

    with pytest.raises(ProviderConfigError, match="'repositories'"):
        load_registry(config_path=config, settings=mock_settings)


def test_rejects_invalid_yaml_syntax(tmp_path, mock_settings):
    config = _write_config(tmp_path, "connections: [unterminated\n")

    with pytest.raises(ProviderConfigError, match="invalid YAML"):
        load_registry(config_path=config, settings=mock_settings)
