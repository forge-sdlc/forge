"""Provider-neutral source control contracts.

Normalized data models and the SourceControlProvider protocol
that every adapter (GitHub, later GitLab) implements. This module has no
I/O and no provider-specific imports.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable


class Provider(StrEnum):
    """Source control providers Forge knows the name of."""

    GITHUB = "github"
    GITLAB = "gitlab"


class ChangeRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class ReviewState(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    PENDING = "pending"
    DISMISSED = "dismissed"


class CheckStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CheckConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"
    NONE = "none"


class EventKind(StrEnum):
    CR_OPENED = "cr_opened"
    CR_UPDATED = "cr_updated"
    CR_CLOSED = "cr_closed"
    CR_MERGED = "cr_merged"
    CHECK_UPDATED = "check_updated"
    COMMENT_CREATED = "comment_created"
    REVIEW_SUBMITTED = "review_submitted"
    PUSH = "push"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Actor:
    login: str
    is_bot: bool


@dataclass(frozen=True)
class RepositoryRef:
    id: str
    provider: Provider
    connection: str
    namespace: str
    default_branch: str
    change_request_mode: Literal["fork", "direct"]


@dataclass(frozen=True)
class Connection:
    name: str
    provider: Provider
    base_url: str
    credential_env: str
    webhook_secret_env: str
    ca_path: str | None = None
    allowed_namespaces: list[str] | None = None


@dataclass(frozen=True)
class ChangeRequestIdentity:
    connection: str
    repository_id: str
    native_id: str | int | None = None


@dataclass
class ChangeRequest:
    identity: ChangeRequestIdentity
    url: str
    title: str
    body: str
    state: ChangeRequestState
    source_branch: str
    target_branch: str
    head_sha: str = ""
    draft: bool = False
    # Whether create_change_request just created this change request, as opposed
    # to returning a pre-existing one for the same head/base pair. Meaningless
    # (and always True) for get/update_change_request results.
    created: bool = True


@dataclass
class ReviewComment:
    id: str
    body: str
    author: str
    path: str | None = None
    line: int | None = None
    resolved: bool = False
    in_reply_to: str | None = None


@dataclass
class Review:
    id: str
    state: ReviewState
    body: str
    author: str
    comments: list[ReviewComment] = field(default_factory=list)


@dataclass
class CheckRun:
    name: str
    status: CheckStatus
    conclusion: CheckConclusion
    url: str | None = None
    logs_url: str | None = None
    output: dict[str, str] = field(default_factory=dict)  # title/summary/text


@dataclass(frozen=True)
class GitCredentials:
    """Everything local `git` (clone/fetch/push over HTTPS) needs to talk to
    a connection's host, independent of that connection's API surface.

    Returned by ``SourceControlProvider.get_git_credentials`` -- a cheap,
    side-effect-free derivation from the adapter's already-resolved
    Connection/credential, safe to call from any code path (including one
    reconstructing a workspace from persisted state, not a fresh API call).
    """

    host: str  # bare host, e.g. "github.com" or "ghe.example.com" -- no scheme
    # repr=False: keep the raw token out of the dataclass's default repr, so
    # an unhandled exception's traceback or a stray `logger.debug(credentials)`
    # doesn't print it -- this module deliberately avoids a pydantic/SecretStr
    # dependency (see the module docstring), so a repr guard is the
    # lightweight equivalent.
    token: str = field(repr=False)
    # CA bundle for a connection's self-signed TLS certificate (GitHub
    # Enterprise Server). None for the common case (public GitHub or a CA
    # trusted by the default store).
    ca_path: str | None = None


@dataclass
class WriteTarget:
    clone_url: str
    push_remote_name: str
    head_ref: str  # the source branch to open the change request from
    base_branch: str
    # Fork identity, populated only for change_request_mode == "fork"; None for
    # "direct". Callers that push via local git need these to add the fork remote
    # and build the provider-native cross-fork head ref.
    fork_owner: str | None = None
    fork_repo: str | None = None


@dataclass
class NormalizedEvent:
    id: str
    kind: EventKind
    repo_ref: RepositoryRef
    actor: Actor
    received_at: datetime
    change_request: ChangeRequest | None = None
    comment: ReviewComment | None = None
    review: Review | None = None
    check: CheckRun | None = None
    # Status of the check *suite* a CHECK_UPDATED event belongs to, independent
    # of `check` (which is only populated for individual check_run events, not
    # check_suite events). None when the event has no associated suite status.
    check_suite_status: CheckStatus | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceControlProvider(Protocol):
    """Provider-neutral operations every source control adapter implements."""

    async def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool: ...

    async def parse_webhook(
        self, headers: dict[str, str], body: bytes, resolver: "RepositoryResolver"
    ) -> NormalizedEvent: ...

    async def resolve_default_branch(self, repo_ref: RepositoryRef) -> str: ...

    async def get_git_credentials(self, repo_ref: RepositoryRef) -> GitCredentials:
        """Host/token/CA for local `git` operations against this connection.

        Side-effect-free (no API call) -- a pure derivation from the
        adapter's already-resolved Connection/credential. Declared async
        (despite never awaiting) to match the rest of this protocol: a sync
        method here is a footgun against AsyncMock-based test doubles, which
        silently hand back an unawaited coroutine instead of raising. Safe to
        call from any code path, including one reconstructing GitOperations
        from persisted workflow state rather than a fresh repo_ref
        resolution.
        """
        ...

    async def ensure_write_target(self, repo_ref: RepositoryRef) -> WriteTarget: ...

    async def create_change_request(
        self,
        repo_ref: RepositoryRef,
        target: WriteTarget,
        title: str,
        body: str,
        draft: bool = False,
    ) -> ChangeRequest: ...

    async def get_change_request(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> ChangeRequest: ...

    async def update_change_request(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        *,
        title: str | None = None,
        body: str | None = None,
        state: ChangeRequestState | None = None,
    ) -> ChangeRequest: ...

    async def create_comment(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, body: str
    ) -> ReviewComment: ...

    async def reply_to_comment(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        comment_id: str,
        body: str,
    ) -> ReviewComment: ...

    async def get_review_threads(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        """Submission-level review verdicts (approve/request-changes/comment); comments empty."""
        ...

    async def get_review_thread_comments(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
    ) -> list[Review]:
        """Unresolved inline diff-comment threads; one Review per thread, comments populated."""
        ...

    async def get_review_comments_for_submission(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, review_id: str
    ) -> list[ReviewComment]:
        """Inline comments from one specific review submission.

        Scoped to a single review, unlike get_review_thread_comments (every
        unresolved thread regardless of which review raised it) -- avoids
        pulling in stale comments from prior review rounds on the same PR.
        """
        ...

    async def get_checks(self, repo_ref: RepositoryRef, ref: str) -> list[CheckRun]: ...

    async def get_check_logs(self, repo_ref: RepositoryRef, check: CheckRun) -> str: ...

    async def get_check_artifacts(
        self, repo_ref: RepositoryRef, check: CheckRun
    ) -> list[tuple[str, bytes]]: ...

    async def get_file(self, repo_ref: RepositoryRef, path: str, ref: str) -> str: ...

    async def put_file(
        self,
        repo_ref: RepositoryRef,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> None: ...

    async def create_branch(self, repo_ref: RepositoryRef, name: str, base: str) -> None: ...

    async def get_authenticated_identity(self, repo_ref: RepositoryRef) -> Actor: ...

    async def close(self) -> None:
        """Release this adapter's underlying HTTP client/connection pool.

        Called once by Registry.aclose() at process shutdown for every
        adapter it has cached -- not per-operation. A no-op if the adapter
        never lazily constructed a client (i.e. was never actually used).
        """
        ...


@dataclass(frozen=True)
class ResolvedRepository:
    """Resolving an identifier yields a repository, its connection, and (if a provider has
    registered one) an adapter instance. `adapter` is None until a provider registers a
    factory (see registry.register_adapter_factory, added in Task 5)."""

    repo_ref: RepositoryRef
    connection: Connection
    adapter: SourceControlProvider | None = None


class RepositoryResolver(Protocol):
    """Structural interface that registry.Registry satisfies.

    Declared here (not imported from registry.py) so SourceControlProvider.parse_webhook
    can reference it without contracts.py importing registry.py.
    """

    def resolve(
        self, identifier: str, provider_hint: Provider | None = None
    ) -> ResolvedRepository: ...
