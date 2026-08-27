"""Tests for Registry.resolve() and adapter factory registration."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from forge.integrations.source_control import registry as registry_module
from forge.integrations.source_control.contracts import Provider
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError
from forge.integrations.source_control.registry import (
    IMPLICIT_GITHUB_CONNECTION_NAME,
    load_registry,
    register_adapter_factory,
)


@pytest.fixture(autouse=True)
def _clean_adapter_factories(monkeypatch):
    """Each test starts with no adapter factories registered."""
    monkeypatch.setattr(registry_module, "_ADAPTER_FACTORIES", {})


def _write_config(tmp_path, content: str):
    path = tmp_path / "repos.yaml"
    path.write_text(content)
    return path


def test_resolve_by_explicit_repository_id(tmp_path, mock_settings, monkeypatch):
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
    change_request_mode: direct
""",
    )
    registry = load_registry(config_path=config, settings=mock_settings)

    resolved = registry.resolve("payments-api")

    assert resolved.repo_ref.id == "payments-api"
    assert resolved.repo_ref.namespace == "platform/payments"
    assert resolved.connection.name == "acme-gitlab"


def test_resolve_by_explicit_namespace_with_provider_hint(tmp_path, mock_settings, monkeypatch):
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
""",
    )
    registry = load_registry(config_path=config, settings=mock_settings)

    resolved = registry.resolve("platform/payments", provider_hint=Provider.GITLAB)

    assert resolved.repo_ref.id == "payments-api"


def test_resolve_falls_back_to_implicit_github_connection(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)

    resolved = registry.resolve("acme/payments")

    assert resolved.repo_ref.id == "acme/payments"
    assert resolved.repo_ref.namespace == "acme/payments"
    assert resolved.repo_ref.provider is Provider.GITHUB
    assert resolved.repo_ref.change_request_mode == "fork"
    assert resolved.connection.name == IMPLICIT_GITHUB_CONNECTION_NAME
    assert resolved.connection.allowed_namespaces is None


def test_resolve_raises_not_found_for_provider_with_no_implicit_default(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)

    with pytest.raises(NotFoundError):
        registry.resolve("platform/payments", provider_hint=Provider.GITLAB)


def test_resolve_raises_provider_config_error_when_implicit_credential_env_unset(
    tmp_path, mock_settings
):
    settings = mock_settings.model_copy(update={"github_token": SecretStr("")})
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=settings)

    with pytest.raises(ProviderConfigError, match="GITHUB_TOKEN"):
        registry.resolve("acme/payments")


def test_resolve_uses_registered_adapter_factory(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)
    sentinel_adapter = object()
    register_adapter_factory(Provider.GITHUB, lambda _connection: sentinel_adapter)

    resolved = registry.resolve("acme/payments")

    assert resolved.adapter is sentinel_adapter


def test_resolve_adapter_is_none_when_no_factory_registered(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)

    resolved = registry.resolve("acme/payments")

    assert resolved.adapter is None


def test_resolve_caches_one_adapter_per_connection(tmp_path, mock_settings):
    """Repeated resolve() calls against the same connection must reuse the
    same adapter instance (and its underlying HTTP client), not construct a
    fresh one -- constructing on every call leaks a connection pool per call."""
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)
    factory_calls: list[object] = []
    register_adapter_factory(
        Provider.GITHUB, lambda connection: factory_calls.append(connection) or object()
    )

    first = registry.resolve("acme/payments").adapter
    second = registry.resolve("acme/payments").adapter

    assert first is second
    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_aclose_closes_every_cached_adapter(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)
    adapter = AsyncMock()
    register_adapter_factory(Provider.GITHUB, lambda _connection: adapter)
    registry.resolve("acme/payments")

    await registry.aclose()

    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_is_a_noop_when_nothing_was_ever_resolved(tmp_path, mock_settings):
    registry = load_registry(config_path=tmp_path / "missing.yaml", settings=mock_settings)

    await registry.aclose()  # must not raise


def test_get_registry_is_cached():
    registry_module.get_registry.cache_clear()
    try:
        first = registry_module.get_registry()
        second = registry_module.get_registry()
        assert first is second
    finally:
        registry_module.get_registry.cache_clear()
