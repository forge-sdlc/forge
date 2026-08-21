"""GitLab REST API v4 client for merge request and repository operations."""

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_API_BASE_URL = "https://gitlab.com/api/v4"


def encode_project_id(namespace: str) -> str:
    """URL-encode a namespace/path project identifier for GitLab's :id param."""
    return quote(namespace, safe="")


class GitLabClient:
    """Async client for the GitLab REST API v4.

    Unlike GitHubClient, there is no zero-config Settings fallback: GitLab
    has no implicit default connection (see registry.py), so every
    GitLabClient is always constructed with an explicitly-resolved
    credential.
    """

    def __init__(
        self,
        credential: str,
        *,
        base_url: str | None = None,
        ca_path: str | None = None,
    ):
        self._credential = credential
        self.base_url = base_url or _DEFAULT_API_BASE_URL
        self._ca_path = ca_path
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"PRIVATE-TOKEN": self._credential},
                timeout=30.0,
                verify=self._ca_path or True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_project(self, namespace: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(f"/projects/{encode_project_id(namespace)}")
        response.raise_for_status()
        return response.json()

    async def get_authenticated_user(self) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get("/user")
        response.raise_for_status()
        return response.json()
