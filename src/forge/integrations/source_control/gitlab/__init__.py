"""GitLab source control adapter."""

from forge.config import get_settings
from forge.integrations.source_control.contracts import Connection, Provider
from forge.integrations.source_control.gitlab.adapter import GitLabAdapter
from forge.integrations.source_control.registry import (
    register_adapter_factory,
    resolve_env_value,
)

__all__ = ["GitLabAdapter"]


def _build_gitlab_adapter(connection: Connection) -> GitLabAdapter:
    """Registry factory: resolve the connection's credential/secret and bind them.

    Unlike GitHub, GitLab has no Settings field for its credential -- Settings
    doesn't model GITLAB_TOKEN, so resolve_env_value's os.environ fallback is
    what actually resolves it here (see resolve_env_value's docstring).
    """
    settings = get_settings()
    credential = resolve_env_value(connection.credential_env, settings)
    webhook_secret = (
        resolve_env_value(connection.webhook_secret_env, settings)
        if connection.webhook_secret_env
        else None
    )
    return GitLabAdapter(
        connection=connection, credential=credential, webhook_secret=webhook_secret
    )


register_adapter_factory(Provider.GITLAB, _build_gitlab_adapter)
