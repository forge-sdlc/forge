"""GitHub source control adapter."""

from forge.config import get_settings
from forge.integrations.source_control.contracts import Connection, Provider
from forge.integrations.source_control.github.adapter import GitHubAdapter
from forge.integrations.source_control.registry import (
    register_adapter_factory,
    resolve_env_value,
)

__all__ = ["GitHubAdapter"]


def _build_github_adapter(connection: Connection) -> GitHubAdapter:
    """Registry factory: resolve the connection's credential/secret and bind them.

    The connection stores only the *names* of the env vars (spec 154). Resolve
    them through Settings-aware lookup so a .env-only GITHUB_TOKEN is honored.
    """
    settings = get_settings()
    credential = resolve_env_value(connection.credential_env, settings)
    webhook_secret = (
        resolve_env_value(connection.webhook_secret_env, settings)
        if connection.webhook_secret_env
        else None
    )
    return GitHubAdapter(
        connection=connection, credential=credential, webhook_secret=webhook_secret
    )


register_adapter_factory(Provider.GITHUB, _build_github_adapter)
