"""GitLab REST API v4 client for merge request and repository operations."""

import asyncio
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

    async def get_fork(self, namespace: str, fork_owner: str) -> dict[str, Any] | None:
        project_path = namespace.rsplit("/", 1)[-1]
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(f'{fork_owner}/{project_path}')}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def create_fork(self, namespace: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post(f"/projects/{encode_project_id(namespace)}/fork")
        response.raise_for_status()
        return response.json()

    async def get_or_create_fork(
        self,
        namespace: str,
        fork_owner: str,
        wait_for_ready: bool = True,
        max_wait_seconds: int = 60,
    ) -> dict[str, Any]:
        existing = await self.get_fork(namespace, fork_owner)
        if existing is not None:
            return existing

        fork = await self.create_fork(namespace)
        if not wait_for_ready:
            return fork

        fork_id = fork["id"]
        client = await self._get_client()
        elapsed = 0
        while fork.get("import_status", "finished") not in (None, "finished"):
            if elapsed >= max_wait_seconds:
                logger.warning(
                    "Fork %s not ready after %ss, proceeding anyway", fork_id, max_wait_seconds
                )
                return fork
            await asyncio.sleep(2)
            elapsed += 2
            response = await client.get(f"/projects/{fork_id}")
            response.raise_for_status()
            fork = response.json()
        return fork

    async def create_merge_request(
        self,
        source_namespace: str,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        target_project_id: int | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        if target_project_id is not None:
            payload["target_project_id"] = target_project_id
        response = await client.post(
            f"/projects/{encode_project_id(source_namespace)}/merge_requests", json=payload
        )
        response.raise_for_status()
        return response.json()

    async def get_merge_request(self, namespace: str, iid: int) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}"
        )
        response.raise_for_status()
        return response.json()

    async def update_merge_request(
        self,
        namespace: str,
        iid: int,
        *,
        title: str | None = None,
        description: str | None = None,
        state_event: str | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if state_event is not None:
            payload["state_event"] = state_event
        response = await client.put(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}", json=payload
        )
        response.raise_for_status()
        return response.json()

    async def create_note(self, namespace: str, iid: int, body: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}/notes",
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def get_discussions(self, namespace: str, iid: int) -> list[dict[str, Any]]:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}/discussions"
        )
        response.raise_for_status()
        return response.json()

    async def reply_to_discussion(
        self, namespace: str, iid: int, discussion_id: str, body: str
    ) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}/discussions/{discussion_id}/notes",
            json={"body": body},
        )
        response.raise_for_status()
        return response.json()

    async def get_approvals(self, namespace: str, iid: int) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}/approvals"
        )
        response.raise_for_status()
        return response.json()

    async def get_commit_statuses(self, namespace: str, ref: str) -> list[dict[str, Any]]:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/repository/commits/{ref}/statuses"
        )
        response.raise_for_status()
        return response.json()

    async def get_job_trace(self, namespace: str, job_id: int) -> str:
        client = await self._get_client()
        response = await client.get(f"/projects/{encode_project_id(namespace)}/jobs/{job_id}/trace")
        response.raise_for_status()
        return response.text

    async def get_job_artifacts(self, namespace: str, job_id: int) -> bytes | None:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/jobs/{job_id}/artifacts"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content
