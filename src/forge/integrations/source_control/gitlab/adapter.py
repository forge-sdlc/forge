"""GitLab implementation of the SourceControlProvider protocol."""

import logging

from forge.integrations.gitlab.client import GitLabClient
from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequestState,
    Connection,
    GitCredentials,
    RepositoryRef,
)
from forge.integrations.source_control.errors import ProviderConfigError
from forge.integrations.source_control.http_errors import translate_provider_errors

logger = logging.getLogger(__name__)

_translate = translate_provider_errors("GitLab")

_DEFAULT_API_BASE_URL = "https://gitlab.com/api/v4"
_DEFAULT_WEB_BASE_URL = "https://gitlab.com"

_CR_STATE_MAP: dict[str, ChangeRequestState] = {
    "opened": ChangeRequestState.OPEN,
    "closed": ChangeRequestState.CLOSED,
    "merged": ChangeRequestState.MERGED,
    "locked": ChangeRequestState.OPEN,
}


def _web_base_url(connection: Connection) -> str:
    """Derive the git/web host from a connection's API base_url.

    Public GitLab's API root is https://gitlab.com/api/v4; a self-managed
    instance's is https://gitlab.example.com/api/v4. Both share the same
    host as their web/git root, so this only strips the /api/v4 suffix.
    """
    base = (connection.base_url or "").rstrip("/")
    if not base or base == _DEFAULT_API_BASE_URL:
        return _DEFAULT_WEB_BASE_URL
    if base.endswith("/api/v4"):
        return base[: -len("/api/v4")]
    return base


class GitLabAdapter:
    """GitLab implementation of SourceControlProvider protocol."""

    def __init__(
        self,
        connection: Connection,
        credential: str | None = None,
        webhook_secret: str | None = None,
        client: GitLabClient | None = None,
    ):
        self._connection = connection
        self._credential = credential
        self._webhook_secret = webhook_secret
        self._client: GitLabClient | None = client

    def _get_client(self) -> GitLabClient:
        if self._client is None:
            if self._credential is None:
                raise ProviderConfigError(
                    f"GitLabAdapter for connection '{self._connection.name}' has no "
                    "credential configured; GitLab has no implicit default connection."
                )
            self._client = GitLabClient(
                credential=self._credential,
                base_url=self._connection.base_url or None,
                ca_path=self._connection.ca_path,
            )
        return self._client

    @_translate
    async def resolve_default_branch(self, repo_ref: RepositoryRef) -> str:
        client = self._get_client()
        project = await client.get_project(repo_ref.namespace)
        return project.get("default_branch", "main")

    async def get_git_credentials(self, _repo_ref: RepositoryRef) -> GitCredentials:
        web_base = _web_base_url(self._connection)
        host = web_base.removeprefix("https://").removeprefix("http://")
        if self._credential is None:
            raise ProviderConfigError(
                f"GitLabAdapter for connection '{self._connection.name}' has no "
                "credential configured; cannot derive git credentials."
            )
        return GitCredentials(
            host=host, token=self._credential, ca_path=self._connection.ca_path, url_user="oauth2"
        )

    @_translate
    async def get_authenticated_identity(self, _repo_ref: RepositoryRef) -> Actor:
        client = self._get_client()
        user = await client.get_authenticated_user()
        username = user.get("username", "")
        return Actor(login=username, is_bot="bot" in username.lower())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
