"""Repository/connection registry.

Loads an optional repos.yaml-shaped config file and resolves identifiers
(explicit repository ids or provider-native namespaces) to a repository,
connection, and — once a provider has registered one — its
adapter.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import SecretStr

from forge.config import Settings, get_settings
from forge.integrations.source_control.contracts import (
    Connection,
    Provider,
    RepositoryRef,
    ResolvedRepository,
    SourceControlProvider,
)
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError

logger = logging.getLogger(__name__)

IMPLICIT_GITHUB_CONNECTION_NAME = "github-default"

AdapterFactory = Callable[[Connection], SourceControlProvider]
_ADAPTER_FACTORIES: dict[Provider, AdapterFactory] = {}


def register_adapter_factory(provider: Provider, factory: AdapterFactory) -> None:
    """Register the adapter constructor a provider's connections resolve to.

    Called by each provider's integration package at import time (the GitHub
    adapter registers itself in segment 2) — contracts.py and registry.py stay
    free of provider-specific imports.
    """
    _ADAPTER_FACTORIES[provider] = factory


@dataclass(frozen=True)
class _ImplicitConnection:
    """An implicit connection paired with whether its credential is usable right now."""

    connection: Connection
    configured: bool


def _build_implicit_connections(settings: Settings) -> dict[Provider, _ImplicitConnection]:
    """The zero-config GitHub connection every unregistered GitHub namespace resolves to.

    `configured` is checked against `settings.github_token` rather than
    `os.environ`, since Settings can load GITHUB_TOKEN from .env without ever
    adding it to the environment.
    """
    return {
        Provider.GITHUB: _ImplicitConnection(
            connection=Connection(
                name=IMPLICIT_GITHUB_CONNECTION_NAME,
                provider=Provider.GITHUB,
                base_url="https://api.github.com",
                credential_env="GITHUB_TOKEN",
                webhook_secret_env="GITHUB_WEBHOOK_SECRET",
                ca_path=None,
                allowed_namespaces=None,
            ),
            configured=bool(settings.github_token.get_secret_value()),
        )
    }


class Registry:
    """Resolves repository identifiers against configured connections and repositories."""

    def __init__(
        self,
        connections: dict[str, Connection],
        repositories: dict[str, RepositoryRef],
        implicit_connections: dict[Provider, _ImplicitConnection],
    ) -> None:
        self._connections = connections
        self._repositories = repositories
        self._implicit_connections = implicit_connections
        self._namespace_index: dict[tuple[Provider, str], RepositoryRef] = {
            (repo.provider, repo.namespace): repo for repo in repositories.values()
        }
        # One adapter instance per connection for this Registry's lifetime
        # (itself a process-wide singleton via get_registry()), so repeated
        # resolve() calls against the same connection reuse its underlying
        # HTTP client/connection pool instead of leaking a new one each time.
        # Keyed by connection name, which is unique across both explicit
        # repos.yaml connections and the implicit per-provider defaults.
        self._adapter_cache: dict[str, SourceControlProvider] = {}

    def get_connection(self, name: str) -> Connection | None:
        return self._connections.get(name)

    def get_repository(self, repository_id: str) -> RepositoryRef | None:
        return self._repositories.get(repository_id)

    def resolve(self, identifier: str, provider_hint: Provider | None = None) -> ResolvedRepository:
        """Resolve an explicit repository id or a provider-native namespace.

        Explicit repos.yaml ids are tried first. Anything else is treated as a
        namespace under provider_hint (defaulting to GitHub, since that's the
        only provider a bare namespace has ever meant). A namespace with no
        explicit repositories: entry falls back to that provider's implicit
        default connection; a provider with no implicit default raises
        NotFoundError.
        """
        repo_ref = self._repositories.get(identifier)
        if repo_ref is not None:
            return self._build_resolved(repo_ref, self._connections[repo_ref.connection])

        provider = provider_hint or Provider.GITHUB
        repo_ref = self._namespace_index.get((provider, identifier))
        if repo_ref is not None:
            return self._build_resolved(repo_ref, self._connections[repo_ref.connection])

        implicit_entry = self._implicit_connections.get(provider)
        if implicit_entry is None:
            raise NotFoundError(
                f"'{identifier}' does not match a registered repository, and "
                f"'{provider}' has no implicit default connection"
            )
        if not implicit_entry.configured:
            raise ProviderConfigError(
                f"'{identifier}' resolves to the implicit '{provider}' connection, but "
                f"credential_env '{implicit_entry.connection.credential_env}' is not set"
            )

        implicit_connection = implicit_entry.connection
        implicit_ref = RepositoryRef(
            id=identifier,
            provider=provider,
            connection=implicit_connection.name,
            namespace=identifier,
            default_branch="main",
            change_request_mode="fork",
        )
        return self._build_resolved(implicit_ref, implicit_connection)

    def _build_resolved(
        self, repo_ref: RepositoryRef, connection: Connection
    ) -> ResolvedRepository:
        adapter = self._adapter_cache.get(connection.name)
        if adapter is None:
            factory = _ADAPTER_FACTORIES.get(connection.provider)
            if factory is not None:
                adapter = factory(connection)
                self._adapter_cache[connection.name] = adapter
        return ResolvedRepository(repo_ref=repo_ref, connection=connection, adapter=adapter)

    async def aclose(self) -> None:
        """Close every adapter this Registry has cached.

        Call once at process shutdown (FastAPI lifespan, worker shutdown) --
        not per-request. Safe to call even if some/all adapters were never
        actually used (their close() is a no-op in that case).
        """
        for adapter in self._adapter_cache.values():
            await adapter.close()


def resolve_env_value(name: str, settings: Settings) -> str | None:
    """Look up a named env var, preferring the matching Settings field.

    A field Settings models (e.g. GITHUB_TOKEN -> settings.github_token) must
    be read through Settings rather than os.environ: BaseSettings loads .env
    values directly without ever exporting them into the process environment
    (see _build_implicit_connections). Names Settings doesn't model fall back
    to os.environ, since repos.yaml connections can reference credentials
    (e.g. for a provider without a dedicated Settings field) Settings never
    claimed ownership of.
    """
    field_name = name.lower()
    if field_name in type(settings).model_fields:
        value = getattr(settings, field_name)
        if isinstance(value, SecretStr):
            return value.get_secret_value() or None
        return str(value) if value else None
    return os.environ.get(name)


def _parse_connections(raw: dict[str, Any], settings: Settings) -> dict[str, Connection]:
    connections: dict[str, Connection] = {}
    for name, entry in raw.items():
        if name == IMPLICIT_GITHUB_CONNECTION_NAME:
            raise ProviderConfigError(
                f"connection '{name}' collides with the reserved implicit connection name "
                f"'{IMPLICIT_GITHUB_CONNECTION_NAME}'; choose a different name"
            )
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
        if not resolve_env_value(credential_env, settings):
            raise ProviderConfigError(
                f"connection '{name}' references credential_env '{credential_env}', "
                "which is not set"
            )

        # webhook_secret_env is intentionally optional here: a connection used
        # only for API operations (git push, PR creation) with no inbound
        # webhook has no secret to configure. A connection that *does* receive
        # webhooks but omits it fails closed at request time instead of at
        # startup -- GitHubAdapter.verify_webhook rejects every delivery when
        # no secret is configured, logging a warning each time.
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

    connections = _parse_connections(connections_raw, settings)
    repositories = _parse_repositories(repositories_raw, connections)
    implicit_connections = _build_implicit_connections(settings)
    logger.info(
        "Loaded source-control registry from %s: %d connection(s), %d repositor(y/ies)",
        path,
        len(connections),
        len(repositories),
    )
    return Registry(
        connections=connections,
        repositories=repositories,
        implicit_connections=implicit_connections,
    )


@lru_cache
def get_registry() -> Registry:
    """Get the cached, process-wide registry loaded from settings.forge_repos_config_path.

    Cached for the life of the process: repos.yaml edits require a restart to
    take effect (see CLAUDE.md).
    """
    return load_registry()
