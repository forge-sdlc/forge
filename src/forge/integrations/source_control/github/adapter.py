"""GitHub implementation of the SourceControlProvider protocol."""

import base64
import functools
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Connection,
    EventKind,
    GitCredentials,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    RepositoryResolver,
    Review,
    ReviewComment,
    ReviewState,
    WriteTarget,
)
from forge.integrations.source_control.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    SourceControlError,
    TransientProviderError,
)

logger = logging.getLogger(__name__)

_REVIEW_STATE_MAP: dict[str, ReviewState] = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
    "COMMENTED": ReviewState.COMMENTED,
    "PENDING": ReviewState.PENDING,
    "DISMISSED": ReviewState.DISMISSED,
}

_CHECK_STATUS_MAP: dict[str, CheckStatus] = {
    "queued": CheckStatus.QUEUED,
    "in_progress": CheckStatus.IN_PROGRESS,
    "completed": CheckStatus.COMPLETED,
}

_CHECK_CONCLUSION_MAP: dict[str, CheckConclusion] = {
    "success": CheckConclusion.SUCCESS,
    "failure": CheckConclusion.FAILURE,
    "timed_out": CheckConclusion.FAILURE,
    "action_required": CheckConclusion.FAILURE,
    "cancelled": CheckConclusion.CANCELLED,
    "skipped": CheckConclusion.SKIPPED,
    "neutral": CheckConclusion.NEUTRAL,
}

_GITHUB_ACTIONS_APP_SLUG = "github-actions"
_DEFAULT_API_BASE_URL = "https://api.github.com"
_DEFAULT_WEB_BASE_URL = "https://github.com"


def _web_base_url(connection: Connection) -> str:
    """Derive the git/web host from a connection's API base_url.

    Public GitHub's API and web hosts differ (api.github.com vs github.com).
    GitHub Enterprise Server's API root is the same host with an /api/v3
    suffix, so its web root is that host with the suffix stripped.
    """
    base = (connection.base_url or "").rstrip("/")
    if not base or base == _DEFAULT_API_BASE_URL:
        return _DEFAULT_WEB_BASE_URL
    if base.endswith("/api/v3"):
        return base[: -len("/api/v3")]
    return base


def _looks_like_stale_sha_error(response: httpx.Response) -> bool:
    """Best-effort check that a 422 from the Contents API is a stale-sha
    conflict, not an unrelated validation failure (branch protection, the
    path being a directory, etc.) that ConflictError's "retry with a fresh
    read" guidance would misrepresent.
    """
    try:
        message = str(response.json().get("message", "")).lower()
    except Exception:
        return False
    return "sha" in message


def _translate_provider_errors(func):
    """Translate a raw ``httpx.HTTPStatusError`` into the neutral error hierarchy.

    Applied to every adapter method that calls the GitHub API, so the boundary
    contract documented on errors.py -- "nothing above the adapter layer
    touches a provider's own exception types directly" -- actually holds
    everywhere, not just in the couple of methods that used to hand-roll this.
    Methods that already translate specific statuses themselves (get_check_logs,
    put_file) re-raise anything they don't recognize, which this then maps too.
    Statuses with no generic neutral mapping (404, 409, 422, ...) are left for
    callers to handle themselves and propagate unchanged.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise AuthenticationError(f"GitHub rejected the request: {exc}") from exc
            if status == 429:
                retry_after = exc.response.headers.get("Retry-After")
                try:
                    parsed_retry_after = float(retry_after) if retry_after else None
                except ValueError:
                    parsed_retry_after = None
                raise RateLimitedError(
                    f"GitHub rate-limited the request: {exc}",
                    retry_after=parsed_retry_after,
                ) from exc
            if status >= 500:
                raise TransientProviderError(f"GitHub returned {status}: {exc}") from exc
            raise
        except httpx.TransportError as exc:
            # Covers httpx.TimeoutException, ConnectError, and other network-level
            # failures -- all subclasses of TransportError.
            raise TransientProviderError(f"GitHub request failed: {exc}") from exc

    return wrapper


def _require_native_id(identity: ChangeRequestIdentity) -> int:
    """Coerce identity.native_id to an int, rejecting a missing/None value.

    A None native_id (e.g. from an identity built off a malformed webhook
    payload) must not silently become PR number 0 and produce a confusing
    404 from GitHub — fail clearly at the adapter boundary instead.
    """
    if identity.native_id is None:
        raise ValueError(f"ChangeRequestIdentity has no native_id: {identity}")
    return int(identity.native_id)


class GitHubAdapter:
    """GitHub implementation of SourceControlProvider protocol."""

    def __init__(
        self,
        connection: Connection,
        credential: str | None = None,
        webhook_secret: str | None = None,
        client: GitHubClient | None = None,
    ):
        """Initialize GitHub adapter.

        Args:
            connection: Connection configuration
            credential: Already-resolved GitHub token to use for API calls. This
                constructor does not itself read the environment variable named by
                connection.credential_env; the caller is responsible for resolving
                that value and passing it in here. If None, the lazily-constructed
                client falls back to the process-wide Settings/GITHUB_TOKEN.
            webhook_secret: Already-resolved webhook secret used to verify inbound
                webhooks. This constructor does not itself read the environment
                variable named by connection.webhook_secret_env; the caller is
                responsible for resolving that value and passing it in here.
            client: Pre-built GitHubClient to use instead of lazily constructing one.
                Primarily for tests that need to inject a mocked httpx.AsyncClient.
        """
        self._connection = connection
        self._credential = credential
        self._webhook_secret = webhook_secret
        self._client: GitHubClient | None = client

    def _get_client(self) -> GitHubClient:
        """Get the injected GitHub client, or lazily construct one.

        When no client was injected, a new GitHubClient is built using this
        adapter's configured credential (if any) so that adapter instances
        configured for different connections/credentials don't all collapse
        onto the process-wide Settings/GITHUB_TOKEN.
        """
        if self._client is None:
            if self._credential is not None:
                settings = get_settings().model_copy(
                    update={"github_token": SecretStr(self._credential)}
                )
                self._client = GitHubClient(
                    settings=settings,
                    base_url=self._connection.base_url or None,
                    ca_path=self._connection.ca_path,
                )
            else:
                self._client = GitHubClient(
                    base_url=self._connection.base_url or None,
                    ca_path=self._connection.ca_path,
                )
        return self._client

    async def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify GitHub webhook signature.

        Args:
            headers: Request headers (must include X-Hub-Signature-256)
            body: Raw request body bytes

        Returns:
            True if signature is valid, False otherwise
        """
        if not self._webhook_secret:
            logger.warning(
                "Webhook verification failed: no webhook secret configured for this connection"
            )
            return False

        signature_header = headers.get("X-Hub-Signature-256", "")
        if not signature_header.startswith("sha256="):
            return False

        expected_signature = signature_header[len("sha256=") :]

        computed = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()

        return hmac.compare_digest(computed, expected_signature)

    async def parse_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        resolver: RepositoryResolver,
    ) -> NormalizedEvent:
        """Parse GitHub webhook into NormalizedEvent.

        Args:
            headers: Request headers (must include X-GitHub-Event, X-GitHub-Delivery)
            body: Raw request body bytes
            resolver: Registry for resolving repository references

        Returns:
            Normalized event
        """
        event_type = headers.get("X-GitHub-Event", "")
        event_id = headers.get("X-GitHub-Delivery", "")

        payload = json.loads(body.decode())

        repo_namespace = payload.get("repository", {}).get("full_name", "")

        resolved = resolver.resolve(repo_namespace, provider_hint=Provider.GITHUB)
        repo_ref = resolved.repo_ref

        sender = payload.get("sender", {})
        sender_login = sender.get("login", "")
        actor = Actor(
            login=sender_login,
            is_bot=sender.get("type") == "Bot" or "[bot]" in sender_login,
        )

        kind = self._map_event_kind(event_type, payload)

        change_request = None
        pr_payload = payload.get("pull_request")
        issue_pr_stub = payload.get("issue", {}).get("pull_request")
        check_pr_stub = self._extract_check_event_pull_request(payload, event_type)
        if pr_payload is not None:
            change_request = self._map_change_request(pr_payload, repo_ref=repo_ref)
        elif issue_pr_stub is not None:
            issue = payload["issue"]
            # The `issue` payload on an issue_comment event has no `merged` field,
            # so a comment on an already-merged PR is reported as CLOSED rather
            # than MERGED here. Callers that need to distinguish the two must
            # fetch the PR directly.
            change_request = ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection=repo_ref.connection,
                    repository_id=repo_ref.id,
                    native_id=issue.get("number"),
                ),
                url=issue.get("html_url", ""),
                title=issue.get("title", ""),
                body=issue.get("body", "") or "",
                state=self._map_pr_state(issue.get("state"), False),
                source_branch="",
                target_branch="",
                head_sha="",
                draft=False,
            )
        elif check_pr_stub is not None:
            change_request = ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection=repo_ref.connection,
                    repository_id=repo_ref.id,
                    native_id=check_pr_stub.get("number"),
                ),
                url="",
                title="",
                body="",
                state=ChangeRequestState.OPEN,
                source_branch=check_pr_stub.get("head", {}).get("ref", ""),
                target_branch=check_pr_stub.get("base", {}).get("ref", ""),
                head_sha=check_pr_stub.get("head", {}).get("sha", ""),
                draft=False,
            )

        comment = None
        if event_type in ("issue_comment", "pull_request_review_comment") and "comment" in payload:
            raw_comment = payload["comment"]
            in_reply_to = raw_comment.get("in_reply_to_id")
            comment = self._map_review_comment(
                raw_comment, in_reply_to=str(in_reply_to) if in_reply_to is not None else None
            )

        review = None
        if event_type == "pull_request_review" and "review" in payload:
            review = self._map_review(payload["review"])

        check = None
        if event_type == "check_run" and "check_run" in payload:
            check = self._map_check_run(payload["check_run"])

        check_suite_status = self._extract_check_suite_status(payload, event_type)

        return NormalizedEvent(
            id=event_id,
            kind=kind,
            repo_ref=repo_ref,
            actor=actor,
            received_at=datetime.now(UTC),
            change_request=change_request,
            comment=comment,
            review=review,
            check=check,
            check_suite_status=check_suite_status,
            raw=payload,
        )

    def _extract_check_suite_status(self, payload: dict, event_type: str) -> CheckStatus | None:
        """Status of the check_suite a check_run/check_suite webhook belongs to.

        check_suite events carry the status on the suite itself; check_run
        events carry it on the run's nested check_suite. Used by worker.py to
        gate CI-cycle completeness without reading the raw GitHub payload
        directly.
        """
        if event_type == "check_suite":
            raw_status = payload.get("check_suite", {}).get("status")
        elif event_type == "check_run":
            raw_status = payload.get("check_run", {}).get("check_suite", {}).get("status")
        else:
            raw_status = None
        return _CHECK_STATUS_MAP.get(raw_status) if raw_status else None

    def _map_event_kind(self, event_type: str, payload: dict) -> EventKind:
        """Map GitHub event type to normalized EventKind."""
        action = payload.get("action", "")

        if event_type == "pull_request":
            if action == "opened":
                return EventKind.CR_OPENED
            if action in ("synchronize", "edited", "reopened"):
                return EventKind.CR_UPDATED
            if action == "closed":
                if payload.get("pull_request", {}).get("merged", False):
                    return EventKind.CR_MERGED
                return EventKind.CR_CLOSED

        if event_type in ("check_run", "check_suite"):
            return EventKind.CHECK_UPDATED

        if event_type in ("issue_comment", "pull_request_review_comment"):
            if action == "created":
                return EventKind.COMMENT_CREATED
            return EventKind.UNKNOWN

        if event_type == "pull_request_review":
            if action == "submitted":
                return EventKind.REVIEW_SUBMITTED
            return EventKind.UNKNOWN

        if event_type == "push":
            return EventKind.PUSH

        return EventKind.UNKNOWN

    def _map_pr_state(self, state: str | None, merged: bool) -> ChangeRequestState:
        """Map GitHub PR state to normalized ChangeRequestState."""
        if merged:
            return ChangeRequestState.MERGED
        if state == "closed":
            return ChangeRequestState.CLOSED
        return ChangeRequestState.OPEN

    def _extract_check_event_pull_request(self, payload: dict, event_type: str) -> dict | None:
        """Find the "simple pull request" stub a check_run/check_suite webhook
        carries, if any. check_suite events list PRs on the suite itself;
        check_run events list them on the run, falling back to the run's
        nested check_suite (both are real GitHub payload shapes)."""
        if event_type == "check_suite":
            pull_requests = payload.get("check_suite", {}).get("pull_requests", [])
        elif event_type == "check_run":
            check_run = payload.get("check_run", {})
            pull_requests = check_run.get("pull_requests") or check_run.get("check_suite", {}).get(
                "pull_requests", []
            )
        else:
            pull_requests = []
        if len(pull_requests) > 1:
            # Multiple open PRs can share a head branch/SHA (stacked PRs,
            # re-targeted base branch). We have no reliable way to disambiguate
            # here, so the first entry is used and the rest are silently
            # ignored -- log so a misattributed CI status is traceable.
            logger.warning(
                "check event lists %d pull_requests; using the first (#%s) "
                "and ignoring the rest: %s",
                len(pull_requests),
                pull_requests[0].get("number"),
                [pr.get("number") for pr in pull_requests[1:]],
            )
        return pull_requests[0] if pull_requests else None

    @_translate_provider_errors
    async def resolve_default_branch(self, repo_ref: RepositoryRef) -> str:
        """Get the default branch name for a repository.

        Args:
            repo_ref: Repository reference

        Returns:
            Default branch name (e.g. "main")
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()
        repo_data = await client.get_repository(owner, repo)
        return repo_data.get("default_branch", "main")

    async def get_git_credentials(self, _repo_ref: RepositoryRef) -> GitCredentials:
        """Host/token/CA for local `git` operations against this connection.

        Pure derivation from this adapter's already-resolved Connection and
        credential -- no API call, so it's safe to call from any code path,
        including one reconstructing GitOperations from persisted state.

        Args:
            _repo_ref: Repository reference (unused -- credentials are
                per-connection, not per-repo; part of the protocol for
                providers that might scope credentials more narrowly).
        """
        web_base = _web_base_url(self._connection)
        host = web_base.removeprefix("https://").removeprefix("http://")
        token = self._credential or get_settings().github_token.get_secret_value()
        return GitCredentials(host=host, token=token, ca_path=self._connection.ca_path)

    @_translate_provider_errors
    async def ensure_write_target(self, repo_ref: RepositoryRef) -> WriteTarget:
        """Ensure a place to push commits exists before opening a change request.

        For ``change_request_mode == "direct"`` the upstream repository itself is
        the write target and nothing needs to be created. For the default
        ``"fork"`` mode, a fork is created (or reused) and synced with upstream so
        the fork's default branch matches before Forge branches off it.

        Args:
            repo_ref: Repository reference.

        Returns:
            WriteTarget describing where to push and which branch to base off.

        Raises:
            ConflictError: In fork mode, when the fork has diverged from upstream
                and GitHub's merge-upstream API cannot fast-forward it.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        web_base = _web_base_url(self._connection)

        if repo_ref.change_request_mode == "direct":
            return WriteTarget(
                clone_url=f"{web_base}/{owner}/{repo}.git",
                push_remote_name="origin",
                head_ref=f"forge/{owner}/{repo}",
                base_branch=repo_ref.default_branch,
            )

        client = self._get_client()

        fork = await client.get_or_create_fork(owner, repo)
        fork_owner = fork["owner"]["login"]
        fork_name = fork["name"]

        synced = await client.sync_fork_with_upstream(
            fork_owner,
            fork_name,
            branch=repo_ref.default_branch,
        )
        if not synced:
            raise ConflictError(
                f"Fork {fork_owner}/{fork_name}:{repo_ref.default_branch} has "
                f"diverged from upstream {owner}/{repo} and cannot be auto-synced."
            )

        clone_url = fork.get("clone_url") or f"{web_base}/{fork_owner}/{fork_name}.git"
        return WriteTarget(
            clone_url=clone_url,
            push_remote_name="origin",
            head_ref=f"forge/{owner}/{repo}",
            base_branch=repo_ref.default_branch,
            fork_owner=fork_owner,
            fork_repo=fork_name,
        )

    @_translate_provider_errors
    async def create_change_request(
        self,
        repo_ref: RepositoryRef,
        target: WriteTarget,
        title: str,
        body: str,
        draft: bool = False,
    ) -> ChangeRequest:
        """Create a pull request (or reuse an existing one for the same branches).

        Args:
            repo_ref: Repository reference.
            target: Write target produced by ensure_write_target; supplies the
                head/base branches to open the PR against.
            title: PR title.
            body: PR description.
            draft: Whether to open the PR as a draft.

        Returns:
            The created (or already-existing) change request. When GitHub reports
            a PR already exists for the head/base pair, the client returns the
            existing PR (created=False) and it is mapped and returned as-is.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        head = f"{target.fork_owner}:{target.head_ref}" if target.fork_owner else target.head_ref
        result = await client.create_pull_request(
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            head=head,
            base=target.base_branch,
            draft=draft,
        )
        return self._map_change_request(result.pr, repo_ref=repo_ref, created=result.created)

    @_translate_provider_errors
    async def get_change_request(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> ChangeRequest:
        """Fetch a pull request by identity.

        Args:
            repo_ref: Repository reference.
            identity: Change request identity carrying the PR number in native_id.

        Returns:
            The change request, preserving the identity passed in.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        pr = await client.get_pull_request(owner, repo, _require_native_id(identity))
        return self._map_change_request(pr, identity=identity)

    @_translate_provider_errors
    async def update_change_request(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        *,
        title: str | None = None,
        body: str | None = None,
        state: ChangeRequestState | None = None,
    ) -> ChangeRequest:
        """Update a pull request's title, body, and/or state.

        Args:
            repo_ref: Repository reference.
            identity: Change request identity carrying the PR number in native_id.
            title: New title, if changing.
            body: New body, if changing.
            state: New state, if changing. Only OPEN and CLOSED are settable via
                GitHub's PR update endpoint.

        Returns:
            The updated change request, preserving the identity passed in.

        Raises:
            ValueError: If state is ChangeRequestState.MERGED. GitHub's PR update
                (PATCH) endpoint cannot set "merged"; merging is a distinct
                operation via the merge endpoint (not modelled here). Raising is
                preferred over silently ignoring the request or silently closing
                the PR instead, both of which would misrepresent what happened.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        gh_state: str | None = None
        if state is not None:
            if state == ChangeRequestState.MERGED:
                raise ValueError(
                    "Cannot set change request state to MERGED via update_change_request; "
                    "GitHub's PR update endpoint only supports 'open' or 'closed'. "
                    "Merging requires the separate merge operation."
                )
            gh_state = state.value

        pr = await client.update_pull_request(
            owner=owner,
            repo=repo,
            pr_number=_require_native_id(identity),
            title=title,
            body=body,
            state=gh_state,
        )
        return self._map_change_request(pr, identity=identity)

    def _map_change_request(
        self,
        pr: dict,
        *,
        repo_ref: RepositoryRef | None = None,
        identity: ChangeRequestIdentity | None = None,
        created: bool = False,
    ) -> ChangeRequest:
        """Map a GitHub PR dict into a ChangeRequest.

        Exactly one of ``repo_ref`` (to construct a fresh identity from the PR
        number) or ``identity`` (to preserve a caller-supplied identity) must be
        given. ``created`` is only meaningful from create_change_request, which
        passes through whether GitHub actually opened a new PR or returned a
        pre-existing one for the same head/base pair.
        """
        if repo_ref is not None and identity is not None:
            raise ValueError("_map_change_request accepts repo_ref or identity, not both")
        if identity is None:
            if repo_ref is None:
                raise ValueError("_map_change_request requires either repo_ref or identity")
            identity = ChangeRequestIdentity(
                connection=repo_ref.connection,
                repository_id=repo_ref.id,
                native_id=pr.get("number"),
            )

        return ChangeRequest(
            identity=identity,
            url=pr.get("html_url", ""),
            title=pr.get("title", ""),
            body=pr.get("body", "") or "",
            state=self._map_pr_state(pr.get("state"), pr.get("merged", False)),
            source_branch=pr.get("head", {}).get("ref", ""),
            target_branch=pr.get("base", {}).get("ref", ""),
            head_sha=pr.get("head", {}).get("sha", ""),
            draft=pr.get("draft", False),
            created=created,
        )

    @_translate_provider_errors
    async def create_comment(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, body: str
    ) -> ReviewComment:
        """Create a general (PR-level) comment on a change request.

        The protocol's create_comment carries no path/line/commit_id, so this
        can only post a general issue-style comment on the PR conversation, not
        an inline diff comment. The client prepends the bot prefix internally.

        Args:
            repo_ref: Repository reference.
            identity: Change request identity carrying the PR number in native_id.
            body: Comment text.

        Returns:
            The created comment mapped to a ReviewComment.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        comment = await client.create_issue_comment(
            owner=owner,
            repo=repo,
            issue_number=_require_native_id(identity),
            body=body,
        )
        return self._map_review_comment(comment)

    @_translate_provider_errors
    async def reply_to_comment(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        comment_id: str,
        body: str,
    ) -> ReviewComment:
        """Reply within the review thread containing an existing review comment.

        Uses GitHub's review-comment reply endpoint so the reply lands in the
        actual thread rather than as a detached PR-level comment. The client
        prepends the bot prefix internally.

        Args:
            repo_ref: Repository reference.
            identity: Change request identity carrying the PR number in native_id.
            comment_id: ID of the review comment to reply to.
            body: Reply text.

        Returns:
            The created reply mapped to a ReviewComment, with in_reply_to set to
            the parent comment_id.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        comment = await client.reply_to_review_comment(
            owner=owner,
            repo=repo,
            pr_number=_require_native_id(identity),
            comment_id=int(comment_id),
            body=body,
        )
        return self._map_review_comment(comment, in_reply_to=comment_id)

    def _map_review_comment(
        self, comment: dict, *, in_reply_to: str | None = None
    ) -> ReviewComment:
        """Map a GitHub comment API response into a ReviewComment."""
        return ReviewComment(
            id=str(comment.get("id", "")),
            body=comment.get("body", "") or "",
            # GitHub returns "user": null for comments from a deleted account, so a
            # present-but-null key must not be treated as "absent" (.get's default
            # only applies when the key itself is missing).
            author=(comment.get("user") or {}).get("login", ""),
            path=comment.get("path"),
            line=comment.get("line"),
            in_reply_to=in_reply_to,
        )

    @_translate_provider_errors
    async def get_review_threads(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        """Get the formal review submissions on a pull request.

        Despite the protocol name, this returns GitHub's review-level verdicts
        (approve / request-changes / comment), each mapped to a Review with its
        submission-level ReviewState.

        Each Review's ``comments`` is intentionally left empty. GitHub's inline
        review comments (fetched via get_pull_request_review_threads /
        get_pull_request_review_comments) don't carry a reliable link back to
        which review submission they belong to in the data GitHubClient
        currently exposes — the REST comments payload has a
        ``pull_request_review_id`` field, but the existing thread-reconciliation
        methods don't preserve it. Attaching comments to the correct review
        isn't possible without deeper client changes that are out of scope here.

        Args:
            repo_ref: Repository reference.
            identity: Change request identity carrying the PR number in native_id.

        Returns:
            List of Review objects (one per submitted review), with empty comments.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        reviews_data = await client.get_reviews(owner, repo, _require_native_id(identity))
        return [self._map_review(review) for review in reviews_data]

    def _map_review(self, review: dict) -> Review:
        """Map a GitHub review dict into a Review.

        Shared by get_review_threads (REST /reviews, uppercase state strings
        like "APPROVED") and parse_webhook (pull_request_review webhooks,
        lowercase state strings like "approved") -- uppercasing before the
        lookup makes both inputs safe; it's a no-op for the already-uppercase
        REST case.
        """
        gh_state = (review.get("state") or "").upper()
        state = _REVIEW_STATE_MAP.get(gh_state, ReviewState.COMMENTED)
        return Review(
            id=str(review.get("id", "")),
            state=state,
            body=review.get("body", "") or "",
            # GitHub returns "user": null for a deleted account, so a
            # present-but-null key must not be treated as absent.
            author=(review.get("user") or {}).get("login", ""),
            comments=[],
        )

    @_translate_provider_errors
    async def get_review_thread_comments(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        """Return unresolved inline review threads as Reviews with populated comments.

        One Review per thread: Review.id is the thread id, Review.comments are the
        inline diff comments. Wraps the client's GraphQL->REST thread
        reconciliation (which already filters resolved/outdated threads).
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()
        threads = await client.get_pull_request_review_threads(
            owner, repo, _require_native_id(identity)
        )
        reviews: list[Review] = []
        for thread in threads:
            comments = [
                ReviewComment(
                    id=str(c.get("comment_id") or ""),
                    body=c.get("body", "") or "",
                    author=c.get("author", "") or "",
                    path=thread.get("path"),
                    line=thread.get("line"),
                )
                for c in thread.get("comments", [])
            ]
            reviews.append(
                Review(
                    id=str(thread.get("thread_id") or ""),
                    state=ReviewState.COMMENTED,
                    body="",
                    author=comments[0].author if comments else "",
                    comments=comments,
                )
            )
        return reviews

    @_translate_provider_errors
    async def get_review_comments_for_submission(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, review_id: str
    ) -> list[ReviewComment]:
        """Inline comments from one specific review submission (REST, not the
        thread-reconciliation GraphQL query get_review_thread_comments uses)."""
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()
        comments = await client.get_review_comments(
            owner, repo, _require_native_id(identity), int(review_id)
        )
        return [
            ReviewComment(
                id=str(c.get("id", "")),
                body=c.get("body", "") or "",
                # GitHub returns "user": null for comments from a deleted account.
                author=(c.get("user") or {}).get("login", ""),
                path=c.get("path"),
                # GitHub returns "line": null for comments anchored to a diff
                # position that's no longer part of the current diff (e.g. an
                # outdated review comment); original_line/position still carry
                # the location in that case.
                line=c.get("line") or c.get("original_line") or c.get("position"),
            )
            for c in comments
        ]

    @_translate_provider_errors
    async def get_checks(self, repo_ref: RepositoryRef, ref: str) -> list[CheckRun]:
        """Get all CI check results for a ref.

        The underlying client combines two GitHub CI surfaces into one list:
        GitHub Actions check runs and legacy commit statuses (Prow, etc.). Both
        are mapped into CheckRun here.

        Only check runs created by the GitHub Actions app (``app.slug ==
        "github-actions"``) have logs fetchable via the Actions job-logs
        endpoint. A check-run id is NOT an Actions job id, though -- they address
        different resources -- so what gets stored in ``logs_url`` for those is
        the workflow *run* id parsed from ``details_url``. get_check_logs later
        lists that run's jobs and matches this check by name to recover the real
        Actions job id the logs endpoint needs. Check runs from other GitHub
        Apps using the Checks API (CodeQL, Codecov, Vercel, etc.) and commit
        statuses (Prow, etc.) get ``logs_url = None`` -- the former aren't
        Actions jobs, the latter have no logs endpoint at all -- so
        get_check_logs has nothing to fetch for them.

        Status/conclusion map from GitHub's string values. An unrecognized or
        missing conclusion becomes CheckConclusion.NONE. An unrecognized or
        missing status is inferred from the conclusion: a present conclusion
        implies the check finished (CheckStatus.COMPLETED), otherwise it is
        treated as still running (CheckStatus.IN_PROGRESS).

        Args:
            repo_ref: Repository reference.
            ref: Git ref (commit SHA, branch, or tag).

        Returns:
            List of CheckRun objects.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        entries = await client.get_check_runs(owner=owner, repo=repo, ref=ref)
        return [self._map_check_run(entry) for entry in entries]

    def _map_check_run(self, entry: dict) -> CheckRun:
        """Map a GitHub check-run/commit-status dict into a CheckRun.

        Shared by get_checks (REST, one call per entry in the combined
        check-runs/commit-statuses list) and parse_webhook (a single
        check_run webhook payload) -- see get_checks' docstring for the
        logs_url/app-slug and status/conclusion inference rules this applies.
        """
        gh_conclusion = entry.get("conclusion") or ""
        conclusion = _CHECK_CONCLUSION_MAP.get(gh_conclusion, CheckConclusion.NONE)

        gh_status = entry.get("status") or ""
        status = _CHECK_STATUS_MAP.get(gh_status)
        if status is None:
            # Missing/unrecognized status: infer from whether the check has a
            # conclusion. A concluded check is done; otherwise assume running.
            status = (
                CheckStatus.COMPLETED
                if conclusion is not CheckConclusion.NONE
                else CheckStatus.IN_PROGRESS
            )

        # Only GitHub Actions check runs have fetchable logs, and the logs
        # endpoint keys off the Actions *job* id -- a different resource from
        # this check run's own id. What is recoverable here is the workflow
        # *run* id (from details_url); get_check_logs uses it to list the run's
        # jobs and match this check by name to the real job id. Store the run id
        # in logs_url as that resolution key; None means "no fetchable logs".
        is_actions_check = (entry.get("app") or {}).get("slug") == _GITHUB_ACTIONS_APP_SLUG
        run_id = (
            self._parse_workflow_run_id(entry.get("details_url", "")) if is_actions_check else None
        )
        logs_url = str(run_id) if run_id is not None else None

        raw_output = entry.get("output") or {}
        output = {
            k: str(raw_output.get(k) or "")
            for k in ("title", "summary", "text")
            if raw_output.get(k)
        }

        return CheckRun(
            name=entry.get("name", ""),
            status=status,
            conclusion=conclusion,
            url=entry.get("html_url", ""),
            logs_url=logs_url,
            output=output,
        )

    @staticmethod
    def _parse_workflow_run_id(details_url: str) -> int | None:
        """Extract the workflow run id from an Actions check run's details_url.

        Actions sets details_url to
        ``https://github.com/{owner}/{repo}/actions/runs/{run_id}`` (sometimes
        with a trailing ``/job/{job_id}``). Returns the run id, or None when the
        URL isn't in that shape -- in which case the check has no resolvable
        logs.
        """
        marker = "/actions/runs/"
        idx = details_url.find(marker)
        if idx == -1:
            return None
        run_segment = details_url[idx + len(marker) :].split("/", 1)[0]
        return int(run_segment) if run_segment.isdigit() else None

    @_translate_provider_errors
    async def get_check_logs(self, repo_ref: RepositoryRef, check: CheckRun) -> str:
        """Fetch the raw logs for a check run.

        Only GitHub Actions-backed check runs have fetchable logs. For those,
        get_checks stored the workflow *run* id in ``logs_url`` -- not the
        check-run id, which is a different resource from the Actions job id the
        logs endpoint requires. This resolves the real job by listing the run's
        jobs and matching the one whose name equals this check's name, then
        fetches that job's logs.

        Legacy commit-status "checks" (Prow, etc.) have no logs endpoint, so
        their ``logs_url`` is None; asking for their logs raises NotFoundError
        because there is genuinely nothing to fetch, rather than returning
        fabricated text.

        Args:
            repo_ref: Repository reference.
            check: The CheckRun to fetch logs for.

        Returns:
            The job log content as plain text.

        Raises:
            NotFoundError: If the check has no ``logs_url`` (no Actions job backs
                it), if no job in the run matches the check's name, or if GitHub
                returns 404 for the job's logs (translating the provider-specific
                httpx.HTTPStatusError into the neutral error so nothing above the
                adapter layer touches httpx directly).
            SourceControlError: If ``logs_url`` is not a numeric run id, or if
                more than one job in the run shares the check's name, so the
                correct logs cannot be chosen unambiguously.
        """
        if not check.logs_url:
            raise NotFoundError(
                f"No logs available for check {check.name!r}: it is not backed by "
                "a GitHub Actions job (e.g. a legacy commit-status check)."
            )

        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        # logs_url should always be a stringified run id set by _map_check_run,
        # but it survives a round-trip through the Redis queue (see
        # queue/models.py) as a raw string, so a malformed or legacy-format
        # entry must surface as a documented SourceControlError rather than an
        # uncaught ValueError leaking out of the adapter.
        try:
            run_id = int(check.logs_url)
        except ValueError as exc:
            raise SourceControlError(
                f"Check {check.name!r} has a non-numeric logs_url {check.logs_url!r}; "
                "expected a GitHub Actions workflow run id."
            ) from exc
        jobs = await client.list_workflow_run_jobs(owner, repo, run_id)
        matches = [job for job in jobs if job.get("name") == check.name]
        if not matches:
            raise NotFoundError(
                f"No Actions job named {check.name!r} was found in workflow run "
                f"{run_id} of {owner}/{repo}; cannot fetch its logs."
            )
        if len(matches) > 1:
            raise SourceControlError(
                f"Workflow run {run_id} of {owner}/{repo} has {len(matches)} jobs named "
                f"{check.name!r}; cannot unambiguously choose whose logs to fetch."
            )

        job_id = matches[0].get("id")
        if job_id is None:
            raise SourceControlError(
                f"Actions job named {check.name!r} in workflow run {run_id} of "
                f"{owner}/{repo} has no id; cannot fetch its logs."
            )

        try:
            return await client.get_job_logs(owner, repo, job_id=int(job_id))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(
                    f"Logs for check {check.name!r} (job {job_id}) were not "
                    f"found in {owner}/{repo}."
                ) from exc
            raise

    @_translate_provider_errors
    async def get_check_artifacts(
        self, repo_ref: RepositoryRef, check: CheckRun
    ) -> list[tuple[str, bytes]]:
        """Download the workflow run's artifacts for an Actions-backed check.

        logs_url holds the workflow run id (see _map_check_run). Non-Actions
        checks have logs_url=None and no artifacts.
        """
        if not check.logs_url:
            return []
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()
        try:
            run_id = int(check.logs_url)
        except ValueError as exc:
            raise SourceControlError(
                f"Check {check.name!r} has a non-numeric logs_url {check.logs_url!r}"
            ) from exc
        results: list[tuple[str, bytes]] = []
        for artifact in await client.get_run_artifacts(owner, repo, run_id):
            artifact_id = artifact.get("id")
            if artifact_id is None:
                logger.warning(
                    "Skipping artifact with no id in run %s of %s/%s", run_id, owner, repo
                )
                continue
            name = artifact.get("name", str(artifact_id))
            zip_bytes = await client.download_artifact_zip(owner, repo, artifact_id)
            results.append((name, zip_bytes))
        return results

    @_translate_provider_errors
    async def get_file(self, repo_ref: RepositoryRef, path: str, ref: str) -> str:
        """Fetch the content of a file at a ref.

        The client's get_file_contents returns file metadata (with base64
        ``content``) on success, or None for a 404. The protocol's return type is
        a plain ``str``, so a missing file must be raised as NotFoundError rather
        than papered over as an empty string.

        Args:
            repo_ref: Repository reference.
            path: File path in the repository.
            ref: Git ref (branch, tag, or commit SHA).

        Returns:
            The file content decoded to a UTF-8 string.

        Raises:
            NotFoundError: If the file does not exist at the given ref.
            SourceControlError: If GitHub omitted the inline base64 content (it
                only inlines content for files <=1MB; larger files come back
                with encoding "none" and an empty content field on an otherwise
                successful response, which must not be mistaken for an empty
                file).
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        result = await client.get_file_contents(owner=owner, repo=repo, path=path, ref=ref)
        if result is None:
            raise NotFoundError(f"File {path!r} not found at ref {ref!r} in {owner}/{repo}.")

        if result.get("encoding") != "base64":
            raise SourceControlError(
                f"File {path!r} at ref {ref!r} in {owner}/{repo} was not returned as inline "
                f"base64 content (encoding={result.get('encoding')!r}, size={result.get('size')}) "
                "-- GitHub only inlines content for files up to 1MB."
            )

        # base64.b64decode tolerates the embedded newlines GitHub's API includes
        # in the encoded content field.
        return base64.b64decode(result["content"]).decode()

    @_translate_provider_errors
    async def put_file(
        self,
        repo_ref: RepositoryRef,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> None:
        """Create or update a file on a branch.

        GitHub's Contents API requires the current file's ``sha`` to update an
        existing file (a create-without-sha 422s if the file already exists), but
        the protocol signature exposes no ``sha``. So the adapter resolves it: it
        looks the file up on the target branch first, passing through the existing
        ``sha`` for an update, or omitting it when the file doesn't exist yet (a
        create). The client base64-encodes ``content`` internally.

        This lookup-then-write is not atomic: if the file changes on ``branch``
        between the lookup and the write (a concurrent writer, a rebase), the
        ``sha`` this method passes is stale and GitHub rejects the write with a
        409, or a 422 whose error message mentions the sha mismatch. Both are
        translated into ConflictError rather than left as a raw httpx error,
        consistent with how ensure_write_target reports a diverged fork. A 422
        NOT related to a stale sha (branch protection, an invalid path, etc.)
        propagates as the raw httpx.HTTPStatusError instead of being
        mislabeled as a conflict -- see _looks_like_stale_sha_error.

        Args:
            repo_ref: Repository reference.
            path: File path in the repository.
            content: File content (plain text).
            message: Commit message.
            branch: Branch to commit to.

        Raises:
            ConflictError: If the file was concurrently modified between the
                sha lookup and the write.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        existing = await client.get_file_contents(owner=owner, repo=repo, path=path, ref=branch)
        sha = existing["sha"] if existing is not None else None

        try:
            await client.create_or_update_file(
                owner=owner,
                repo=repo,
                path=path,
                content=content,
                message=message,
                branch=branch,
                sha=sha,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 409 or (status == 422 and _looks_like_stale_sha_error(exc.response)):
                raise ConflictError(
                    f"File {path!r} on {branch!r} in {owner}/{repo} was concurrently modified "
                    "since it was last read; retry with a fresh read."
                ) from exc
            raise

    @_translate_provider_errors
    async def create_branch(self, repo_ref: RepositoryRef, name: str, base: str) -> None:
        """Create a new branch from a base ref.

        The client swallows a 422 branch-already-exists into a synthesized
        success, so this is idempotent from the adapter's perspective. The
        protocol returns None, so the client's response dict is discarded.

        Args:
            repo_ref: Repository reference.
            name: New branch name.
            base: Base ref to branch from.
        """
        owner, repo = repo_ref.namespace.split("/", 1)
        client = self._get_client()

        await client.create_branch(owner=owner, repo=repo, branch_name=name, base=base)

    @_translate_provider_errors
    async def get_authenticated_identity(self, _repo_ref: RepositoryRef) -> Actor:
        """Get the authenticated user's identity.

        Args:
            _repo_ref: Repository reference (unused but part of the protocol)

        Returns:
            Actor representing the authenticated user
        """
        client = self._get_client()
        user_data = await client.get_authenticated_user()
        login = user_data.get("login", "")
        is_bot = user_data.get("type") == "Bot" or "[bot]" in login
        return Actor(login=login, is_bot=is_bot)

    async def close(self) -> None:
        """Close the underlying GitHubClient's httpx.AsyncClient, if one was
        ever lazily constructed."""
        if self._client is not None:
            await self._client.close()
