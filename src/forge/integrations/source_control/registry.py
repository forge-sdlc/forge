"""Repository/connection registry.

Loads an optional repos.yaml-shaped config file and resolves identifiers
(explicit repository ids or provider-native namespaces) to a repository,
connection, and — once a provider has registered one — its
adapter.
"""

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from forge.config import Settings, get_settings
from forge.integrations.source_control.contracts import Connection, Provider, RepositoryRef
from forge.integrations.source_control.errors import ProviderConfigError


class Registry:
    """Resolves repository identifiers against configured connections and repositories."""

    def __init__(
        self,
        connections: dict[str, Connection],
        repositories: dict[str, RepositoryRef],
    ) -> None:
        self._connections = connections
        self._repositories = repositories

    def get_connection(self, name: str) -> Connection | None:
        return self._connections.get(name)

    def get_repository(self, repository_id: str) -> RepositoryRef | None:
        return self._repositories.get(repository_id)


def _parse_connections(raw: dict[str, Any]) -> dict[str, Connection]:
    connections: dict[str, Connection] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ProviderConfigError(f"connection '{name}' must be a mapping")
        if "provider" not in entry:
            raise ProviderConfigError(f"connection '{name}' is missing 'provider'")
        try:
            provider = Provider(entry["provider"])
        except ValueError as exc:
            raise ProviderConfigError(
                f"connection '{name}' has unknown provider '{entry['provider']}'"
            ) from exc

        credential_env = entry.get("credential_env")
        if not credential_env:
            raise ProviderConfigError(f"connection '{name}' is missing 'credential_env'")
        if not os.environ.get(credential_env):
            raise ProviderConfigError(
                f"connection '{name}' references credential_env '{credential_env}', "
                "which is not set"
            )

        allowed_namespaces = entry.get("allowed_namespaces")
        if allowed_namespaces is not None and (
            not isinstance(allowed_namespaces, list)
            or not all(isinstance(namespace, str) for namespace in allowed_namespaces)
        ):
            raise ProviderConfigError(
                f"connection '{name}' has invalid 'allowed_namespaces': must be a list of strings"
            )

        connections[name] = Connection(
            name=name,
            provider=provider,
            base_url=entry.get("base_url", ""),
            credential_env=credential_env,
            webhook_secret_env=entry.get("webhook_secret_env", ""),
            ca_path=entry.get("ca_path"),
            allowed_namespaces=allowed_namespaces,
        )
    return connections


def _parse_repositories(
    raw: dict[str, Any], connections: dict[str, Connection]
) -> dict[str, RepositoryRef]:
    repositories: dict[str, RepositoryRef] = {}
    seen_namespaces: dict[tuple[Provider, str], str] = {}
    for repo_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise ProviderConfigError(f"repository '{repo_id}' must be a mapping")
        connection_name = entry.get("connection")
        if connection_name not in connections:
            raise ProviderConfigError(
                f"repository '{repo_id}' references unknown connection '{connection_name}'"
            )
        connection = connections[connection_name]

        if "provider" not in entry:
            raise ProviderConfigError(f"repository '{repo_id}' is missing 'provider'")
        try:
            provider = Provider(entry["provider"])
        except ValueError as exc:
            raise ProviderConfigError(
                f"repository '{repo_id}' has unknown provider '{entry['provider']}'"
            ) from exc

        if provider != connection.provider:
            raise ProviderConfigError(
                f"repository '{repo_id}' has provider '{provider}' but its connection "
                f"'{connection_name}' has provider '{connection.provider}'"
            )

        namespace = entry.get("namespace")
        if not namespace:
            raise ProviderConfigError(f"repository '{repo_id}' is missing 'namespace'")

        if (
            connection.allowed_namespaces is not None
            and namespace not in connection.allowed_namespaces
        ):
            raise ProviderConfigError(
                f"repository '{repo_id}' namespace '{namespace}' is not in connection "
                f"'{connection_name}''s allowed_namespaces"
            )

        namespace_key = (provider, namespace)
        if namespace_key in seen_namespaces:
            raise ProviderConfigError(
                f"repository '{repo_id}' and '{seen_namespaces[namespace_key]}' both use "
                f"namespace '{namespace}' for provider '{provider}'"
            )
        seen_namespaces[namespace_key] = repo_id

        change_request_mode = entry.get("change_request_mode", "fork")
        if change_request_mode not in ("fork", "direct"):
            raise ProviderConfigError(
                f"repository '{repo_id}' has invalid change_request_mode '{change_request_mode}'"
            )

        repositories[repo_id] = RepositoryRef(
            id=repo_id,
            provider=provider,
            connection=connection_name,
            namespace=namespace,
            default_branch=entry.get("default_branch", "main"),
            change_request_mode=change_request_mode,
        )
    return repositories


def _read_yaml_mapping(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ProviderConfigError(f"{path}: invalid YAML: {exc}") from exc


def load_registry(
    config_path: str | Path | None = None, settings: Settings | None = None
) -> Registry:
    """Load and validate the repos.yaml-shaped registry config.

    Raises ProviderConfigError on any misconfiguration: unknown provider,
    unknown connection reference, missing credential env var, or a
    repository namespace excluded by its connection's allowed_namespaces.
    A missing config file is not an error — repos.yaml is optional.
    """
    settings = settings or get_settings()
    path = Path(config_path) if config_path is not None else Path(settings.forge_repos_config_path)

    raw = _read_yaml_mapping(path) if path.exists() else {}
    if not isinstance(raw, dict):
        raise ProviderConfigError(f"{path} must contain a YAML mapping at the top level")

    connections_raw = raw.get("connections") or {}
    if not isinstance(connections_raw, dict):
        raise ProviderConfigError(f"{path}: 'connections' must be a mapping")
    repositories_raw = raw.get("repositories") or {}
    if not isinstance(repositories_raw, dict):
        raise ProviderConfigError(f"{path}: 'repositories' must be a mapping")

    connections = _parse_connections(connections_raw)
    repositories = _parse_repositories(repositories_raw, connections)
    return Registry(connections=connections, repositories=repositories)
