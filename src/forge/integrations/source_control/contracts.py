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
    draft: bool = False


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


@dataclass
class WriteTarget:
    clone_url: str
    push_remote_name: str
    head_ref: str
    base_branch: str


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
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceControlProvider(Protocol):
    """Provider-neutral operations every source control adapter implements."""

    async def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool: ...

    async def parse_webhook(
        self, headers: dict[str, str], body: bytes, resolver: "RepositoryResolver"
    ) -> NormalizedEvent: ...

    async def resolve_default_branch(self, repo_ref: RepositoryRef) -> str: ...

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
    ) -> list[Review]: ...

    async def get_checks(self, repo_ref: RepositoryRef, ref: str) -> list[CheckRun]: ...

    async def get_check_logs(self, repo_ref: RepositoryRef, check: CheckRun) -> str: ...

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
