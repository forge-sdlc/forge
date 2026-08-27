from forge.integrations.source_control.contracts import Connection, Provider
from forge.integrations.source_control.gitlab import GitLabAdapter
from forge.integrations.source_control.registry import _ADAPTER_FACTORIES


def test_gitlab_factory_registered_on_import():
    factory = _ADAPTER_FACTORIES.get(Provider.GITLAB)
    assert factory is not None
    conn = Connection(
        name="c",
        provider=Provider.GITLAB,
        base_url="https://gitlab.com/api/v4",
        credential_env="GITLAB_TOKEN",
        webhook_secret_env="GITLAB_WEBHOOK_SECRET",
    )
    adapter = factory(conn)
    assert isinstance(adapter, GitLabAdapter)


def test_factory_resolves_credential_env_via_os_environ(monkeypatch):
    """GitLab has no dedicated Settings field, so credential_env resolution
    must fall back to os.environ (resolve_env_value's documented fallback
    for names Settings doesn't model)."""
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-from-env")
    factory = _ADAPTER_FACTORIES[Provider.GITLAB]
    conn = Connection(
        name="c",
        provider=Provider.GITLAB,
        base_url="https://gitlab.com/api/v4",
        credential_env="GITLAB_TOKEN",
        webhook_secret_env="GITLAB_WEBHOOK_SECRET",
    )

    adapter = factory(conn)

    assert adapter._credential == "glpat-from-env"


def test_registry_resolve_returns_gitlab_adapter_for_explicit_repository(
    mock_settings, monkeypatch, tmp_path
):
    from forge.integrations.source_control.registry import load_registry

    monkeypatch.setenv("GITLAB_TOKEN", "glpat-from-env")
    repos_yaml = tmp_path / "repos.yaml"
    repos_yaml.write_text(
        """
connections:
  gitlab-main:
    provider: gitlab
    base_url: https://gitlab.com/api/v4
    credential_env: GITLAB_TOKEN
repositories:
  acme-gitlab:
    provider: gitlab
    connection: gitlab-main
    namespace: acme/widgets
"""
    )

    registry = load_registry(config_path=repos_yaml, settings=mock_settings)
    resolved = registry.resolve("acme-gitlab")

    assert isinstance(resolved.adapter, GitLabAdapter)
