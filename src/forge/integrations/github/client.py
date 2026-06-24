"""GitHub REST API client for PR and repository operations."""

import logging
from typing import Any

import httpx

from forge.config import Settings, get_settings

logger = logging.getLogger(__name__)

_STATUS_TO_CONCLUSION: dict[str, str] = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "pending": "",
}


def _normalize_commit_status(status: dict[str, Any]) -> dict[str, Any]:
    """Convert a commit-status object to the check-run shape.

    Commit statuses (used by Prow) have a ``state`` field instead of
    ``status``/``conclusion``. This normalizes them so ``evaluate_ci_status``
    can treat all CI results uniformly.
    """
    state = status.get("state", "pending")
    completed = state != "pending"
    return {
        "name": status.get("context", ""),
        "status": "completed" if completed else "in_progress",
        "conclusion": _STATUS_TO_CONCLUSION.get(state, ""),
        "output": {"summary": status.get("description", "")},
        "html_url": status.get("target_url", ""),
    }


class GitHubClient:
    """Async client for GitHub REST API.

    Handles authentication and common operations for repositories,
    pull requests, and code reviews.
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize the GitHub client.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self.base_url = "https://api.github.com"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with authentication."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.settings.github_token.get_secret_value()}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict[str, Any]:
        """Create a new pull request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            title: PR title.
            body: PR description.
            head: Source branch name.
            base: Target branch name.

        Returns:
            API response with PR details.
        """
        client = await self._get_client()
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"Created PR #{data['number']} in {owner}/{repo}")
        return data

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Get pull request details.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            API response with PR details.
        """
        client = await self._get_client()
        response = await client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
        response.raise_for_status()
        return response.json()

    async def create_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
    ) -> dict[str, Any]:
        """Create a review comment on a PR.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            body: Comment text.
            commit_id: Commit SHA to comment on.
            path: File path to comment on.
            line: Line number to comment on.

        Returns:
            API response with comment details.
        """
        client = await self._get_client()
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            json={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
            },
        )
        response.raise_for_status()
        logger.info(f"Created review comment on PR #{pr_number}")
        return response.json()

    async def get_pull_request_review_comments(
        self, owner: str, repo: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Get unresolved inline review comments on a pull request.

        Uses the GraphQL API to fetch review threads with their resolution
        status, returning only comments from threads that are still open.
        Falls back to the REST API (which returns all comments) if GraphQL
        fails.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            List of comment dicts with path, position, and body, from
            unresolved threads only.
        """
        query = """
        query($owner: String!, $repo: String!, $prNumber: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $prNumber) {
              reviewThreads(first: 100) {
                nodes {
                  isResolved
                  isOutdated
                  comments(first: 50) {
                    nodes {
                      path
                      line
                      originalLine
                      body
                      author { login }
                    }
                  }
                }
              }
            }
          }
        }
        """
        try:
            client = await self._get_client()
            response = await client.post(
                "/graphql",
                json={
                    "query": query,
                    "variables": {
                        "owner": owner,
                        "repo": repo,
                        "prNumber": pr_number,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()

            threads = (
                data.get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
                .get("reviewThreads", {})
                .get("nodes", [])
            )

            comments = []
            for thread in threads:
                if thread.get("isResolved") or thread.get("isOutdated"):
                    continue
                for comment in thread.get("comments", {}).get("nodes", []):
                    comments.append(
                        {
                            "path": comment.get("path", ""),
                            "position": comment.get("line") or comment.get("originalLine"),
                            "body": comment.get("body", ""),
                        }
                    )
            return comments

        except Exception as e:
            logger.warning(f"GraphQL review thread fetch failed, falling back to REST: {e}")
            # Fallback: REST API returns all comments (ignores resolved status)
            client = await self._get_client()
            response = await client.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                params={"per_page": 100},
            )
            response.raise_for_status()
            return response.json()

    async def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """Get inline comments from a specific PR review.

        Scoped to a single review submission, avoiding stale comments
        from prior review rounds.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            review_id: The review ID from the webhook payload.

        Returns:
            List of comment dicts with path, line, and body.
        """
        client = await self._get_client()
        response = await client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/comments",
            params={"per_page": 100},
        )
        response.raise_for_status()
        return response.json()

    async def create_issue_comment(
        self, owner: str, repo: str, issue_number: int, body: str
    ) -> dict[str, Any]:
        """Create a general comment on a PR/issue.

        Args:
            owner: Repository owner.
            repo: Repository name.
            issue_number: Issue/PR number.
            body: Comment text.

        Returns:
            API response with comment details.
        """
        client = await self._get_client()
        response = await client.post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        response.raise_for_status()
        logger.info(f"Created comment on issue #{issue_number}")
        return response.json()

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        """Get all CI results for a commit, combining check runs and commit statuses.

        GitHub has two CI APIs:
        - Check runs: used by GitHub Actions and modern CI apps.
        - Commit statuses: used by Prow and other legacy systems.

        Both are fetched and normalized to the check-run shape so callers
        can treat them uniformly.

        Args:
            owner: Repository owner.
            repo: Repository name.
            ref: Git reference (commit SHA, branch, or tag).

        Returns:
            List of normalized CI result dicts with ``status`` and
            ``conclusion`` keys.
        """
        client = await self._get_client()
        results: list[dict[str, Any]] = []

        # --- GitHub Actions / check-run API ---
        response = await client.get(f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
        response.raise_for_status()
        results.extend(response.json().get("check_runs", []))

        # --- Prow / commit-status API ---
        # Statuses are de-duplicated by context; latest entry per context wins.
        statuses_response = await client.get(f"/repos/{owner}/{repo}/commits/{ref}/statuses")
        statuses_response.raise_for_status()
        raw_statuses: list[dict[str, Any]] = statuses_response.json()

        # Keep only the most-recent status per context (API returns newest first).
        seen_contexts: set[str] = set()
        for status in raw_statuses:
            ctx = status.get("context", "")
            if ctx in seen_contexts:
                continue
            seen_contexts.add(ctx)
            results.append(_normalize_commit_status(status))

        return results

    async def get_workflow_run_logs(self, owner: str, repo: str, run_id: int) -> str:
        """Download workflow run logs.

        Args:
            owner: Repository owner.
            repo: Repository name.
            run_id: Workflow run ID.

        Returns:
            Log content as string.
        """
        client = await self._get_client()
        response = await client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    async def get_job_logs(self, owner: str, repo: str, job_id: int | str) -> str:
        """Download logs for a specific Actions job as plain text.

        Args:
            owner: Repository owner.
            repo: Repository name.
            job_id: GitHub Actions job ID.

        Returns:
            Log content as string, or empty string on failure.
        """
        client = await self._get_client()
        response = await client.get(
            f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text

    async def get_run_artifacts(
        self, owner: str, repo: str, run_id: int | str
    ) -> list[dict[str, Any]]:
        """List artifacts produced by a workflow run.

        Args:
            owner: Repository owner.
            repo: Repository name.
            run_id: Workflow run ID.

        Returns:
            List of artifact metadata dicts.
        """
        client = await self._get_client()
        response = await client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
            params={"per_page": 100},
        )
        response.raise_for_status()
        return response.json().get("artifacts", [])

    async def download_artifact_zip(self, owner: str, repo: str, artifact_id: int | str) -> bytes:
        """Download an artifact as a zip archive.

        Args:
            owner: Repository owner.
            repo: Repository name.
            artifact_id: Artifact ID.

        Returns:
            Raw zip bytes.
        """
        client = await self._get_client()
        response = await client.get(
            f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip",
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.content

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        """Get repository details.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            API response with repository details.
        """
        client = await self._get_client()
        response = await client.get(f"/repos/{owner}/{repo}")
        response.raise_for_status()
        return response.json()

    async def get_failed_check_logs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        """Get logs for failed check runs.

        Args:
            owner: Repository owner.
            repo: Repository name.
            ref: Git reference.

        Returns:
            List of failed checks with log excerpts.
        """
        check_runs = await self.get_check_runs(owner, repo, ref)
        failed_checks = []

        for check in check_runs:
            if check.get("conclusion") in ("failure", "cancelled"):
                failed_checks.append(
                    {
                        "name": check.get("name"),
                        "conclusion": check.get("conclusion"),
                        "output": check.get("output", {}),
                        "html_url": check.get("html_url"),
                    }
                )

        return failed_checks

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Update a pull request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            title: New title (optional).
            body: New body (optional).
            state: New state ("open" or "closed", optional).

        Returns:
            API response with updated PR details.
        """
        client = await self._get_client()
        data: dict[str, Any] = {}
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        if state is not None:
            data["state"] = state

        response = await client.patch(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            json=data,
        )
        response.raise_for_status()
        logger.info(f"Updated PR #{pr_number} in {owner}/{repo}")
        return response.json()

    async def get_authenticated_user(self) -> dict[str, Any]:
        """Get the authenticated user's information.

        Returns:
            API response with user details including login name.
        """
        client = await self._get_client()
        response = await client.get("/user")
        response.raise_for_status()
        return response.json()

    async def get_fork(
        self,
        upstream_owner: str,
        upstream_repo: str,
        fork_owner: str | None = None,
    ) -> dict[str, Any] | None:
        """Get an existing fork of a repository.

        Args:
            upstream_owner: Owner of the upstream repository.
            upstream_repo: Name of the upstream repository.
            fork_owner: Owner of the fork. Uses configured or authenticated user if None.

        Returns:
            Fork repository data if exists, None otherwise.
        """
        client = await self._get_client()

        # Determine fork owner
        if not fork_owner:
            fork_owner = self.settings.github_fork_owner
        if not fork_owner:
            user = await self.get_authenticated_user()
            fork_owner = user["login"]

        try:
            response = await client.get(f"/repos/{fork_owner}/{upstream_repo}")
            if response.status_code == 404:
                return None
            response.raise_for_status()

            repo_data = response.json()
            # Verify this is actually a fork of the upstream repo
            if repo_data.get("fork"):
                parent = repo_data.get("parent", {})
                if (
                    parent.get("owner", {}).get("login") == upstream_owner
                    and parent.get("name") == upstream_repo
                ):
                    return repo_data

            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def create_fork(
        self,
        upstream_owner: str,
        upstream_repo: str,
        fork_owner: str | None = None,
    ) -> dict[str, Any]:
        """Create a fork of a repository.

        Args:
            upstream_owner: Owner of the upstream repository.
            upstream_repo: Name of the upstream repository.
            fork_owner: Organization to create fork in (None = user's account).

        Returns:
            API response with fork repository details.
        """
        client = await self._get_client()

        # Determine fork owner for organization forks
        org = fork_owner or self.settings.github_fork_owner

        data: dict[str, Any] = {"default_branch_only": True}
        if org:
            data["organization"] = org

        response = await client.post(
            f"/repos/{upstream_owner}/{upstream_repo}/forks",
            json=data,
        )
        response.raise_for_status()
        fork_data = response.json()
        logger.info(f"Created fork {fork_data['full_name']} from {upstream_owner}/{upstream_repo}")
        return fork_data

    async def get_or_create_fork(
        self,
        upstream_owner: str,
        upstream_repo: str,
        fork_owner: str | None = None,
        wait_for_ready: bool = True,
        max_wait_seconds: int = 60,
    ) -> dict[str, Any]:
        """Get existing fork or create one if it doesn't exist.

        Args:
            upstream_owner: Owner of the upstream repository.
            upstream_repo: Name of the upstream repository.
            fork_owner: Owner for the fork. Uses configured or authenticated user if None.
            wait_for_ready: Wait for fork to be ready after creation.
            max_wait_seconds: Maximum time to wait for fork readiness.

        Returns:
            Fork repository data.
        """
        import asyncio

        # Check for existing fork
        existing = await self.get_fork(upstream_owner, upstream_repo, fork_owner)
        if existing:
            logger.info(f"Found existing fork: {existing['full_name']}")
            return existing

        # Create new fork
        fork = await self.create_fork(upstream_owner, upstream_repo, fork_owner)

        if not wait_for_ready:
            return fork

        # Wait for fork to be ready (GitHub creates forks asynchronously)
        fork_owner_actual = fork["owner"]["login"]
        fork_name = fork["name"]
        client = await self._get_client()

        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > max_wait_seconds:
                logger.warning(
                    f"Fork {fork_owner_actual}/{fork_name} not ready after "
                    f"{max_wait_seconds}s, proceeding anyway"
                )
                return fork

            await asyncio.sleep(2)

            try:
                # Check if we can access the fork's default branch
                response = await client.get(
                    f"/repos/{fork_owner_actual}/{fork_name}/branches/{fork.get('default_branch', 'main')}"
                )
                if response.status_code == 200:
                    logger.info(f"Fork {fork_owner_actual}/{fork_name} is ready")
                    return fork
            except Exception:
                pass

            logger.debug(f"Waiting for fork {fork_owner_actual}/{fork_name} to be ready...")

    async def sync_fork_with_upstream(
        self,
        fork_owner: str,
        fork_repo: str,
        branch: str = "main",
    ) -> bool:
        """Sync a fork's branch with its upstream repository.

        Uses GitHub's merge-upstream API to update the fork.

        Args:
            fork_owner: Owner of the fork.
            fork_repo: Name of the fork repository.
            branch: Branch to sync (default: main).

        Returns:
            True if sync succeeded or was not needed, False on error.
        """
        client = await self._get_client()

        try:
            response = await client.post(
                f"/repos/{fork_owner}/{fork_repo}/merge-upstream",
                json={"branch": branch},
            )

            if response.status_code == 200:
                result = response.json()
                message = result.get("message", "")
                if "up to date" in message.lower():
                    logger.info(f"Fork {fork_owner}/{fork_repo}:{branch} is already up to date")
                else:
                    logger.info(f"Synced fork {fork_owner}/{fork_repo}:{branch} with upstream")
                return True
            elif response.status_code == 409:
                # Conflict - fork has diverged, can't auto-sync
                logger.warning(
                    f"Fork {fork_owner}/{fork_repo}:{branch} has diverged from upstream, "
                    "cannot auto-sync"
                )
                return False
            else:
                response.raise_for_status()
                return True

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to sync fork {fork_owner}/{fork_repo}: {e}")
            return False

    async def get_fork_owner(self) -> str:
        """Get the fork owner (configured or authenticated user).

        Returns:
            GitHub username or organization for forks.
        """
        if self.settings.github_fork_owner:
            return self.settings.github_fork_owner
        user = await self.get_authenticated_user()
        return user["login"]

    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch_name: str,
        base: str = "main",
    ) -> dict[str, Any]:
        """Create a new branch from a base ref.

        Args:
            owner: Repository owner.
            repo: Repository name.
            branch_name: New branch name.
            base: Base branch to branch from.

        Returns:
            API response with ref details.
        """
        client = await self._get_client()

        ref_response = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{base}")
        ref_response.raise_for_status()
        sha = ref_response.json()["object"]["sha"]

        try:
            response = await client.post(
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": sha},
            )
            response.raise_for_status()
            data = response.json()
            logger.info(f"Created branch {branch_name} in {owner}/{repo}")
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                logger.info(f"Branch {branch_name} already exists in {owner}/{repo}")
                return {"ref": f"refs/heads/{branch_name}", "object": {"sha": sha}}
            raise

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file via the Contents API.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path in the repository.
            content: File content (plain text, will be base64-encoded).
            message: Commit message.
            branch: Target branch.
            sha: Existing file SHA (required for updates, omit for creates).

        Returns:
            API response with content details.
        """
        import base64 as b64

        client = await self._get_client()
        body: dict[str, Any] = {
            "message": message,
            "content": b64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        response = await client.put(f"/repos/{owner}/{repo}/contents/{path}", json=body)
        response.raise_for_status()
        data = response.json()
        logger.info(f"{'Updated' if sha else 'Created'} file {path} on {branch} in {owner}/{repo}")
        return data

    async def get_file_contents(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str,
    ) -> dict[str, Any] | None:
        """Get file contents and metadata from a repository.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path in the repository.
            ref: Git ref (branch, tag, or SHA).

        Returns:
            File metadata including sha, or None if not found.
        """
        client = await self._get_client()
        try:
            response = await client.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
