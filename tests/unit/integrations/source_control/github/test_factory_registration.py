from forge.integrations.source_control.contracts import Connection, Provider
from forge.integrations.source_control.github import GitHubAdapter
from forge.integrations.source_control.registry import _ADAPTER_FACTORIES


def test_github_factory_registered_on_import():
    factory = _ADAPTER_FACTORIES.get(Provider.GITHUB)
    assert factory is not None
    conn = Connection(
        name="c",
        provider=Provider.GITHUB,
        base_url="https://api.github.com",
        credential_env="GITHUB_TOKEN",
        webhook_secret_env="GITHUB_WEBHOOK_SECRET",
    )
    adapter = factory(conn)
    assert isinstance(adapter, GitHubAdapter)


def test_registry_resolve_returns_github_adapter(mock_settings):
    from forge.integrations.source_control.registry import load_registry

    registry = load_registry(config_path="/nonexistent/repos.yaml", settings=mock_settings)
    resolved = registry.resolve("acme/widgets")
    assert isinstance(resolved.adapter, GitHubAdapter)
