"""GitLab implementation of the SourceControlProvider protocol."""

import hashlib
import hmac
import json
import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime

import httpx

from forge.integrations.gitlab.client import GitLabClient
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
    ConflictError,
    NotFoundError,
    ProviderConfigError,
    SourceControlError,
)
from forge.integrations.source_control.http_errors import translate_provider_errors

logger = logging.getLogger(__name__)

_translate = translate_provider_errors("GitLab")

_DEFAULT_API_BASE_URL = "https://gitlab.com/api/v4"
_DEFAULT_WEB_BASE_URL = "https://gitlab.com"

_MAX_FORK_PROJECT_CACHE_ENTRIES = 1000


_CR_STATE_MAP: dict[str, ChangeRequestState] = {
    "opened": ChangeRequestState.OPEN,
    "closed": ChangeRequestState.CLOSED,
    "merged": ChangeRequestState.MERGED,
    "locked": ChangeRequestState.OPEN,
}


def _looks_like_stale_commit_error(response: httpx.Response) -> bool:
    try:
        message = str(response.json().get("message", "")).lower()
    except Exception:
        return False
    return "changed since" in message


def _looks_like_branch_exists_error(response: httpx.Response) -> bool:
    try:
        message = str(response.json().get("message", "")).lower()
    except Exception:
        return False
    return "already exists" in message


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


def _require_native_id(identity: ChangeRequestIdentity) -> int:
    """Coerce identity.native_id to an int, rejecting a missing/None value.

    A None native_id (e.g. from an identity built off a malformed webhook
    payload) must not silently become MR iid 0 and produce a confusing
    404 from GitLab -- fail clearly at the adapter boundary instead.
    """
    if identity.native_id is None:
        raise ValueError(f"ChangeRequestIdentity has no native_id: {identity}")
    return int(identity.native_id)


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
        # Three-valued: unknown (None) / known-supported (True) /
        # known-unsupported (False). Cached per adapter instance, which
        # Registry reuses across every project/MR resolved through this
        # connection -- see Registry._adapter_cache.
        self._approvals_supported: bool | None = None
        # Fork-mode MRs run their pipeline in the source (fork) project, not
        # repo_ref.namespace (always the upstream/target project). These
        # caches -- populated whenever an MR's attrs are seen (see
        # _cache_source_project) -- let get_checks/get_check_logs/
        # get_check_artifacts query the project that actually has the
        # commit statuses/job instead of the upstream, which would never
        # see them. This adapter instance is shared across every
        # repo/MR on the connection (see Registry._adapter_cache), so
        # _source_project_by_ref is keyed by (repo_ref.namespace, ref) to
        # avoid two repos with a same-named branch clobbering each
        # other's entry, and both caches are bounded LRUs (evicted via
        # _cache_put) since they are otherwise never evicted for the
        # life of a long-running worker process.
        self._source_project_by_ref: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._project_by_job_id: OrderedDict[int, str] = OrderedDict()

    @staticmethod
    def _cache_put(cache: OrderedDict, key, value, *, max_size: int = _MAX_FORK_PROJECT_CACHE_ENTRIES) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)

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

    @_translate
    async def ensure_write_target(self, repo_ref: RepositoryRef) -> WriteTarget:
        web_base = _web_base_url(self._connection)

        if repo_ref.change_request_mode == "direct":
            return WriteTarget(
                clone_url=f"{web_base}/{repo_ref.namespace}.git",
                push_remote_name="origin",
                head_ref=f"forge/{repo_ref.namespace}",
                base_branch=repo_ref.default_branch,
            )

        client = self._get_client()
        identity = await self.get_authenticated_identity(repo_ref)
        fork = await client.get_or_create_fork(repo_ref.namespace, fork_owner=identity.login)

        fork_namespace = fork["path_with_namespace"]
        fork_owner, fork_repo = fork_namespace.rsplit("/", 1)
        clone_url = fork.get("http_url_to_repo") or f"{web_base}/{fork_namespace}.git"
        return WriteTarget(
            clone_url=clone_url,
            push_remote_name="origin",
            head_ref=f"forge/{repo_ref.namespace}",
            base_branch=repo_ref.default_branch,
            fork_owner=fork_owner,
            fork_repo=fork_repo,
        )

    @_translate
    async def create_change_request(
        self,
        repo_ref: RepositoryRef,
        target: WriteTarget,
        title: str,
        body: str,
        draft: bool = False,
    ) -> ChangeRequest:
        """Create a merge request (or reuse an existing one for the same source branch).

        Unlike GitHub (which 422s), GitLab returns 409 Conflict when an open MR
        already exists for the source branch. On 409, the existing MR is looked
        up (scoped to repo_ref.namespace, which is always the target/upstream
        project regardless of fork/direct mode) and returned with created=False,
        mirroring GitHub's create_pull_request/_map_change_request(...,
        created=False) behavior. If GitLab reports the conflict but no matching
        open MR can be found, the 409 is translated into ConflictError rather
        than left as a raw httpx type.
        """
        client = self._get_client()

        target_project_id = None
        source_namespace = repo_ref.namespace
        if target.fork_owner:
            source_namespace = f"{target.fork_owner}/{target.fork_repo}"
            upstream = await client.get_project(repo_ref.namespace)
            target_project_id = upstream["id"]

        mr_title = (
            f"Draft: {title}" if draft and not title.startswith(("Draft:", "WIP:")) else title
        )
        try:
            result = await client.create_merge_request(
                source_namespace,
                source_branch=target.head_ref,
                target_branch=target.base_branch,
                title=mr_title,
                description=body,
                target_project_id=target_project_id,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            existing = await client.get_merge_requests(
                repo_ref.namespace, source_branch=target.head_ref
            )
            if existing:
                logger.info(
                    f"MR already exists for {target.head_ref} -> {target.base_branch}: "
                    f"!{existing[0].get('iid')} in {repo_ref.namespace}"
                )
                return self._map_change_request(existing[0], repo_ref=repo_ref, created=False)
            raise ConflictError(
                f"GitLab reports a merge request already exists for source branch "
                f"{target.head_ref!r} in {repo_ref.namespace}, but it could not be located "
                "via the merge requests list endpoint."
            ) from exc
        return self._map_change_request(result, repo_ref=repo_ref, created=True)

    @_translate
    async def get_change_request(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> ChangeRequest:
        client = self._get_client()
        mr = await client.get_merge_request(repo_ref.namespace, _require_native_id(identity))
        return self._map_change_request(mr, repo_ref=repo_ref, identity=identity)

    @_translate
    async def update_change_request(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        *,
        title: str | None = None,
        body: str | None = None,
        state: ChangeRequestState | None = None,
    ) -> ChangeRequest:
        client = self._get_client()

        state_event: str | None = None
        if state is not None:
            if state == ChangeRequestState.MERGED:
                raise ValueError(
                    "Cannot set change request state to MERGED via update_change_request; "
                    "GitLab's MR update endpoint only supports 'close' or 'reopen' state_events."
                )
            state_event = "close" if state == ChangeRequestState.CLOSED else "reopen"

        mr = await client.update_merge_request(
            repo_ref.namespace,
            _require_native_id(identity),
            title=title,
            description=body,
            state_event=state_event,
        )
        return self._map_change_request(mr, repo_ref=repo_ref, identity=identity)

    async def verify_webhook(self, headers: dict[str, str], _body: bytes) -> bool:
        """GitLab sends its configured secret verbatim in X-Gitlab-Token
        (no HMAC signing of the body, unlike GitHub)."""
        if not self._webhook_secret:
            logger.warning(
                "Webhook verification failed: no webhook secret configured for this connection"
            )
            return False
        return hmac.compare_digest(headers.get("X-Gitlab-Token", ""), self._webhook_secret)

    async def parse_webhook(
        self, _headers: dict[str, str], body: bytes, resolver: RepositoryResolver
    ) -> NormalizedEvent:
        event_id = hashlib.sha256(body).hexdigest()[:16]
        payload = json.loads(body.decode())
        object_kind = payload.get("object_kind", "")

        repo_namespace = payload.get("project", {}).get("path_with_namespace", "")
        repo_ref = resolver.resolve(repo_namespace, provider_hint=Provider.GITLAB).repo_ref

        actor = self._extract_actor(payload, object_kind)
        kind = self._map_event_kind(object_kind, payload)

        change_request = None
        comment = None
        review = None
        check_suite_status = None

        if object_kind == "merge_request":
            attrs = payload.get("object_attributes", {})
            change_request = self._map_change_request(attrs, repo_ref=repo_ref)
            review = self._map_approval_review(attrs, payload)
        elif object_kind == "note":
            attrs = payload.get("object_attributes", {})
            if attrs.get("noteable_type") == "MergeRequest":
                comment = self._map_note(attrs, actor.login)
                mr = payload.get("merge_request")
                if mr is not None:
                    change_request = self._map_change_request(mr, repo_ref=repo_ref)
        elif object_kind == "pipeline":
            attrs = payload.get("object_attributes", {})
            check_suite_status, _ = self._map_check_status(attrs.get("status", ""))
            mr_stub = payload.get("merge_request")
            if mr_stub is not None:
                change_request = self._map_change_request(mr_stub, repo_ref=repo_ref)

        return NormalizedEvent(
            id=event_id,
            kind=kind,
            repo_ref=repo_ref,
            actor=actor,
            received_at=datetime.now(UTC),
            change_request=change_request,
            comment=comment,
            review=review,
            check_suite_status=check_suite_status,
            raw=payload,
        )

    _APPROVAL_REVIEW_STATE: dict[str, ReviewState] = {
        "approved": ReviewState.APPROVED,
        "approval": ReviewState.APPROVED,
        # GitLab has no formal "un-approve" review state; the withdrawal of an
        # approval is the closest neutral-model equivalent to DISMISSED.
        "unapproved": ReviewState.DISMISSED,
        "unapproval": ReviewState.DISMISSED,
    }

    def _map_approval_review(self, attrs: dict, payload: dict) -> Review | None:
        """Map an MR approval/unapproval action to a Review.

        GitLab's approvals are the only submission-level verdict it has (see
        get_review_threads), so this is the only case parse_webhook populates
        `review` for a merge_request event.
        """
        action = attrs.get("action", "")
        state = self._APPROVAL_REVIEW_STATE.get(action)
        if state is None:
            return None
        return Review(
            id=f"{attrs.get('iid', '')}-{action}",
            state=state,
            body="",
            author=payload.get("user", {}).get("username", ""),
            comments=[],
        )

    def _extract_actor(self, payload: dict, object_kind: str) -> Actor:
        if object_kind == "push":
            username = payload.get("user_username", "")
        else:
            username = payload.get("user", {}).get("username", "")
        return Actor(login=username, is_bot="bot" in username.lower())

    def _map_event_kind(self, object_kind: str, payload: dict) -> EventKind:
        if object_kind == "merge_request":
            action = payload.get("object_attributes", {}).get("action", "")
            return {
                "open": EventKind.CR_OPENED,
                "reopen": EventKind.CR_UPDATED,
                "update": EventKind.CR_UPDATED,
                "close": EventKind.CR_CLOSED,
                "merge": EventKind.CR_MERGED,
                "approved": EventKind.REVIEW_SUBMITTED,
                "unapproved": EventKind.REVIEW_SUBMITTED,
                "approval": EventKind.REVIEW_SUBMITTED,
                "unapproval": EventKind.REVIEW_SUBMITTED,
            }.get(action, EventKind.UNKNOWN)
        if object_kind == "note":
            attrs = payload.get("object_attributes", {})
            return (
                EventKind.COMMENT_CREATED
                if attrs.get("noteable_type") == "MergeRequest"
                else EventKind.UNKNOWN
            )
        if object_kind == "pipeline":
            return EventKind.CHECK_UPDATED
        if object_kind == "push":
            return EventKind.PUSH
        return EventKind.UNKNOWN

    def _map_note(self, attrs: dict, author: str) -> ReviewComment:
        position = attrs.get("position") or {}
        return ReviewComment(
            id=str(attrs.get("id", "")),
            body=attrs.get("note", "") or "",
            author=author,
            path=position.get("new_path"),
            line=position.get("new_line"),
        )

    def _map_change_request(
        self,
        attrs: dict,
        *,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity | None = None,
        created: bool = False,
    ) -> ChangeRequest:
        """Map a GitLab merge request attrs dict into a ChangeRequest.

        ``repo_ref`` is always required, both to namespace the fork-project
        cache (see _cache_source_project) and, when ``identity`` is not
        given, to construct a fresh identity from the MR iid.
        """
        if identity is None:
            identity = ChangeRequestIdentity(
                connection=repo_ref.connection,
                repository_id=repo_ref.id,
                native_id=attrs.get("iid"),
            )
        self._cache_source_project(repo_ref, attrs)
        return ChangeRequest(
            identity=identity,
            url=attrs.get("url", "") or attrs.get("web_url", ""),
            title=attrs.get("title", "") or "",
            body=attrs.get("description", "") or "",
            state=_CR_STATE_MAP.get(attrs.get("state", ""), ChangeRequestState.OPEN),
            source_branch=attrs.get("source_branch", "") or "",
            target_branch=attrs.get("target_branch", "") or "",
            head_sha=(attrs.get("last_commit") or {}).get("id", ""),
            draft=attrs.get("draft", attrs.get("work_in_progress", False)),
            created=created,
        )

    def _cache_source_project(self, repo_ref: RepositoryRef, attrs: dict) -> None:
        """Remember the project a fork-mode MR's commits actually live in.

        GitLab scopes commit statuses/jobs to the project that ran the
        pipeline -- for a fork-mode MR that's the source (fork) project,
        never repo_ref.namespace (always the upstream/target project; see
        get_merge_requests). Keyed by (repo_ref.namespace, ref) -- this
        adapter instance is shared across every repo on the connection
        (see Registry._adapter_cache), so the namespace must be part of
        the key or two repos with a same-named branch (or a shared
        fallback ref like "main") would clobber each other's entry.
        Looked up by whatever ref callers use: the head commit sha, and
        the source branch name as a fallback.
        """
        source_project_id = attrs.get("source_project_id")
        target_project_id = attrs.get("target_project_id")
        if source_project_id is None or source_project_id == target_project_id:
            return
        project_ref = str(source_project_id)
        namespace = repo_ref.namespace
        head_sha = (attrs.get("last_commit") or {}).get("id", "")
        if head_sha:
            self._cache_put(self._source_project_by_ref, (namespace, head_sha), project_ref)
        source_branch = attrs.get("source_branch", "") or ""
        if source_branch:
            self._cache_put(self._source_project_by_ref, (namespace, source_branch), project_ref)

    def _map_check_status(self, status: str) -> tuple[CheckStatus, CheckConclusion]:
        table = {
            "success": (CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
            "failed": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
            "canceled": (CheckStatus.COMPLETED, CheckConclusion.CANCELLED),
            "skipped": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
            "manual": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "created": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "pending": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "scheduled": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "preparing": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "waiting_for_resource": (CheckStatus.QUEUED, CheckConclusion.NONE),
            "running": (CheckStatus.IN_PROGRESS, CheckConclusion.NONE),
        }
        return table.get(status, (CheckStatus.IN_PROGRESS, CheckConclusion.NONE))

    @_translate
    async def create_comment(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, body: str
    ) -> ReviewComment:
        client = self._get_client()
        note = await client.create_note(repo_ref.namespace, _require_native_id(identity), body)
        return self._map_note_response(note)

    @_translate
    async def reply_to_comment(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        comment_id: str,
        body: str,
    ) -> ReviewComment:
        client = self._get_client()
        iid = _require_native_id(identity)
        discussions = await client.get_discussions(repo_ref.namespace, iid)
        discussion_id = next(
            (
                d["id"]
                for d in discussions
                if any(str(n.get("id")) == comment_id for n in d.get("notes", []))
            ),
            None,
        )
        if discussion_id is None:
            raise NotFoundError(
                f"No discussion on MR !{iid} of {repo_ref.namespace} contains note {comment_id!r}."
            )
        note = await client.reply_to_discussion(repo_ref.namespace, iid, discussion_id, body)
        return self._map_note_response(note, in_reply_to=comment_id)

    def _map_note_response(self, note: dict, *, in_reply_to: str | None = None) -> ReviewComment:
        return ReviewComment(
            id=str(note.get("id", "")),
            body=note.get("body", "") or "",
            author=(note.get("author") or {}).get("username", ""),
            path=note.get("position", {}).get("new_path") if note.get("position") else None,
            line=note.get("position", {}).get("new_line") if note.get("position") else None,
            resolved=note.get("resolved", False),
            in_reply_to=in_reply_to,
        )

    @_translate
    async def get_review_threads(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        """GitLab has no submission-level "review" object: this maps the
        Approvals API, so only APPROVED entries are ever returned (or an
        empty list) -- there is no GitLab analog to CHANGES_REQUESTED,
        COMMENTED, PENDING, or DISMISSED at this level.

        Degrades to [] once the Approvals API is confirmed absent on this
        connection (self-managed instances without it, e.g. below
        Premium/Ultimate) rather than raising -- see _approvals_supported.
        """
        iid = _require_native_id(identity)
        if self._approvals_supported is False:
            return []

        client = self._get_client()
        try:
            approvals = await client.get_approvals(repo_ref.namespace, iid)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            try:
                await client.get_merge_request(repo_ref.namespace, iid)
            except httpx.HTTPStatusError as confirm_exc:
                if confirm_exc.response.status_code == 404:
                    # The MR itself doesn't exist -- an ordinary not-found,
                    # not a capability gap. Leave _approvals_supported
                    # untouched and re-raise the original 404.
                    raise exc from confirm_exc
                raise
            self._approvals_supported = False
            logger.info(
                "GitLab connection %r has no Approvals API support; "
                "get_review_threads will return [] for it going forward.",
                self._connection.name,
            )
            return []

        self._approvals_supported = True
        return [
            Review(
                id=str(entry["user"]["id"]),
                state=ReviewState.APPROVED,
                body="",
                author=entry["user"].get("username", ""),
                comments=[],
            )
            for entry in approvals.get("approved_by", [])
        ]

    @_translate
    async def get_review_thread_comments(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        client = self._get_client()
        discussions = await client.get_discussions(repo_ref.namespace, _require_native_id(identity))
        reviews: list[Review] = []
        for discussion in discussions:
            notes = discussion.get("notes", [])
            first = notes[0] if notes else {}
            if (
                first.get("type") != "DiffNote"
                or not first.get("resolvable")
                or first.get("resolved")
            ):
                continue
            comments = [self._map_note_response(n) for n in notes]
            reviews.append(
                Review(
                    id=str(discussion.get("id", "")),
                    state=ReviewState.COMMENTED,
                    body="",
                    author=comments[0].author if comments else "",
                    comments=comments,
                )
            )
        return reviews

    @_translate
    async def get_review_comments_for_submission(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, review_id: str
    ) -> list[ReviewComment]:
        """GitLab has no grouping of comments by review submission; review_id
        is treated as a discussion id (the id space get_review_thread_comments
        returns). An id with no matching discussion returns [] rather than
        raising, since a caller may legitimately pass an approval id from
        get_review_threads, which is a different id space."""
        client = self._get_client()
        discussions = await client.get_discussions(repo_ref.namespace, _require_native_id(identity))
        discussion = next((d for d in discussions if str(d.get("id")) == review_id), None)
        if discussion is None:
            return []
        return [self._map_note_response(n) for n in discussion.get("notes", [])]

    @_translate
    async def get_checks(self, repo_ref: RepositoryRef, ref: str) -> list[CheckRun]:
        client = self._get_client()
        project_ref = self._source_project_by_ref.get((repo_ref.namespace, ref), repo_ref.namespace)
        entries = await client.get_commit_statuses(project_ref, ref)
        return [self._map_commit_status(entry, project_ref) for entry in entries]

    def _map_commit_status(self, entry: dict, project_ref: str) -> CheckRun:
        status, conclusion = self._map_check_status(entry.get("status", ""))
        target_url = entry.get("target_url") or ""
        job_id = self._parse_job_id(target_url)
        if job_id is not None:
            self._cache_put(self._project_by_job_id, job_id, project_ref)
        return CheckRun(
            name=entry.get("name", ""),
            status=status,
            conclusion=conclusion,
            url=target_url,
            logs_url=str(job_id) if job_id is not None else None,
        )

    @staticmethod
    def _parse_job_id(target_url: str) -> int | None:
        match = re.search(r"/-/(?:jobs|builds)/(\d+)", target_url)
        return int(match.group(1)) if match else None

    @staticmethod
    def _require_numeric_logs_url(name: str, logs_url: str) -> int:
        try:
            return int(logs_url)
        except ValueError as exc:
            raise SourceControlError(
                f"Check {name!r} has a non-numeric logs_url {logs_url!r}; "
                "expected a GitLab CI job id."
            ) from exc

    @_translate
    async def get_check_logs(self, repo_ref: RepositoryRef, check: CheckRun) -> str:
        if not check.logs_url:
            raise NotFoundError(
                f"No logs available for check {check.name!r}: it is not backed by "
                "a GitLab CI job (e.g. an external CI integration's commit status)."
            )
        job_id = self._require_numeric_logs_url(check.name, check.logs_url)
        project_ref = self._project_by_job_id.get(job_id, repo_ref.namespace)
        client = self._get_client()
        try:
            return await client.get_job_trace(project_ref, job_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(
                    f"No trace found for check {check.name!r} (job {job_id}); it may have "
                    "expired or been deleted under GitLab's job log retention policy."
                ) from exc
            raise

    @_translate
    async def get_check_artifacts(
        self, repo_ref: RepositoryRef, check: CheckRun
    ) -> list[tuple[str, bytes]]:
        if not check.logs_url:
            return []
        job_id = self._require_numeric_logs_url(check.name, check.logs_url)
        project_ref = self._project_by_job_id.get(job_id, repo_ref.namespace)
        client = self._get_client()
        artifact_bytes = await client.get_job_artifacts(project_ref, job_id)
        if artifact_bytes is None:
            return []
        return [("artifacts.zip", artifact_bytes)]

    @_translate
    async def get_file(self, repo_ref: RepositoryRef, path: str, ref: str) -> str:
        client = self._get_client()
        content = await client.get_file_raw(repo_ref.namespace, path, ref)
        if content is None:
            raise NotFoundError(f"File {path!r} not found at ref {ref!r} in {repo_ref.namespace}.")
        return content

    @_translate
    async def put_file(
        self, repo_ref: RepositoryRef, path: str, content: str, message: str, branch: str
    ) -> None:
        client = self._get_client()
        existing = await client.get_file_metadata(repo_ref.namespace, path, branch)
        try:
            if existing is not None:
                await client.update_file(
                    repo_ref.namespace,
                    path,
                    branch=branch,
                    content=content,
                    commit_message=message,
                    last_commit_id=existing.get("last_commit_id"),
                )
            else:
                await client.create_file(
                    repo_ref.namespace, path, branch=branch, content=content, commit_message=message
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and _looks_like_stale_commit_error(exc.response):
                raise ConflictError(
                    f"File {path!r} on {branch!r} in {repo_ref.namespace} was concurrently "
                    "modified since it was last read; retry with a fresh read."
                ) from exc
            raise

    @_translate
    async def create_branch(self, repo_ref: RepositoryRef, name: str, base: str) -> None:
        client = self._get_client()
        try:
            await client.create_branch(repo_ref.namespace, name, base)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400 and _looks_like_branch_exists_error(exc.response):
                return
            raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
