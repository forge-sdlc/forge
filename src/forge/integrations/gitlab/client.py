"""GitLab REST API v4 client for merge request and repository operations."""

import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from forge.integrations.source_control.errors import SourceControlError, TransientProviderError

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
        """Return ``fork_owner``'s fork of ``namespace``, or None if none exists.

        Looks the fork up through the upstream project's fork list and matches
        on the owning namespace rather than assuming the fork kept the upstream
        project's path. GitLab appends a numeric suffix (e.g. ``repo1``) when a
        same-named project already exists under ``fork_owner`` at fork time, so
        a path-based lookup would 404 and make get_or_create_fork create a
        duplicate fork.
        """
        client = await self._get_client()
        per_page = 100
        page = 1
        while True:
            response = await client.get(
                f"/projects/{encode_project_id(namespace)}/forks",
                params={"page": page, "per_page": per_page},
            )
            response.raise_for_status()
            forks = response.json()
            for fork in forks:
                if fork.get("namespace", {}).get("full_path") == fork_owner:
                    return fork
            if len(forks) < per_page:
                return None
            page += 1

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
            if existing.get("import_status") == "failed":
                raise SourceControlError(
                    f"Fork {existing.get('path_with_namespace', fork_owner)} of {namespace} "
                    "exists but its import failed; remove it in GitLab and retry."
                )
            return existing

        fork = await self.create_fork(namespace)
        if not wait_for_ready:
            return fork

        fork_id = fork["id"]
        client = await self._get_client()
        elapsed = 0
        while fork.get("import_status", "finished") not in (None, "finished"):
            if fork.get("import_status") == "failed":
                raise SourceControlError(
                    f"GitLab reported that fork {fork_id} of {namespace} failed to import."
                )
            if elapsed >= max_wait_seconds:
                raise TransientProviderError(
                    f"Fork {fork_id} of {namespace} was not ready after {max_wait_seconds}s "
                    "(import still in progress)."
                )
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

    async def get_merge_requests(
        self, namespace: str, *, source_branch: str, state: str = "opened"
    ) -> list[dict[str, Any]]:
        """List merge requests for a project, filtered by source branch.

        ``namespace`` is scoped to the project a merge request belongs to,
        which in GitLab's data model is always the *target* project of the
        MR -- for a fork-mode MR, this is the upstream project, not the fork.
        """
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/merge_requests",
            params={"source_branch": source_branch, "state": state},
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
        per_page = 100
        path = f"/projects/{encode_project_id(namespace)}/merge_requests/{iid}/discussions"
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await client.get(path, params={"page": page, "per_page": per_page})
            response.raise_for_status()
            page_results = response.json()
            results.extend(page_results)
            if len(page_results) < per_page:
                return results
            page += 1

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
        per_page = 100
        path = (
            f"/projects/{encode_project_id(namespace)}/repository/commits/"
            f"{encode_project_id(ref)}/statuses"
        )
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await client.get(path, params={"page": page, "per_page": per_page})
            response.raise_for_status()
            page_results = response.json()
            results.extend(page_results)
            if len(page_results) < per_page:
                return results
            page += 1

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

    async def get_file_raw(self, namespace: str, path: str, ref: str) -> str | None:
        client = await self._get_client()
        response = await client.get(
            f"/projects/{encode_project_id(namespace)}/repository/files/{encode_project_id(path)}/raw",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def get_file_metadata(self, namespace: str, path: str, ref: str) -> dict[str, Any] | None:
        """Fetch file metadata via HEAD, which GitLab returns as response
        headers -- avoids downloading the (potentially large) base64-encoded
        file body that a GET to this same endpoint would include."""
        client = await self._get_client()
        response = await client.head(
            f"/projects/{encode_project_id(namespace)}/repository/files/{encode_project_id(path)}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        headers = response.headers
        return {
            "file_name": headers.get("X-Gitlab-File-Name"),
            "file_path": headers.get("X-Gitlab-File-Path"),
            "size": headers.get("X-Gitlab-Size"),
            "encoding": headers.get("X-Gitlab-Encoding"),
            "content_sha256": headers.get("X-Gitlab-Content-Sha256"),
            "ref": headers.get("X-Gitlab-Ref"),
            "blob_id": headers.get("X-Gitlab-Blob-Id"),
            "commit_id": headers.get("X-Gitlab-Commit-Id"),
            "last_commit_id": headers.get("X-Gitlab-Last-Commit-Id"),
            "execute_filemode": headers.get("X-Gitlab-Execute-Filemode"),
        }

    async def create_file(
        self, namespace: str, path: str, *, branch: str, content: str, commit_message: str
    ) -> None:
        client = await self._get_client()
        response = await client.post(
            f"/projects/{encode_project_id(namespace)}/repository/files/{encode_project_id(path)}",
            json={"branch": branch, "content": content, "commit_message": commit_message},
        )
        response.raise_for_status()

    async def update_file(
        self,
        namespace: str,
        path: str,
        *,
        branch: str,
        content: str,
        commit_message: str,
        last_commit_id: str | None,
    ) -> None:
        client = await self._get_client()
        payload: dict[str, Any] = {
            "branch": branch,
            "content": content,
            "commit_message": commit_message,
        }
        if last_commit_id is not None:
            payload["last_commit_id"] = last_commit_id
        response = await client.put(
            f"/projects/{encode_project_id(namespace)}/repository/files/{encode_project_id(path)}",
            json=payload,
        )
        response.raise_for_status()

    async def create_branch(self, namespace: str, branch: str, ref: str) -> None:
        client = await self._get_client()
        response = await client.post(
            f"/projects/{encode_project_id(namespace)}/repository/branches",
            json={"branch": branch, "ref": ref},
        )
        response.raise_for_status()
