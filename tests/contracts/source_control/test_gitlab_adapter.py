"""Tests for GitLab source control adapter."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.integrations.gitlab.client import GitLabClient
from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Connection,
    EventKind,
    Provider,
    RepositoryRef,
    ResolvedRepository,
    ReviewState,
    SourceControlProvider,
    WriteTarget,
)
from forge.integrations.source_control.errors import (
    AuthenticationError,
    ConflictError,
    RateLimitedError,
    SourceControlError,
    TransientProviderError,
)
from forge.integrations.source_control.errors import (
    NotFoundError as SCNotFoundError,
)
from forge.integrations.source_control.gitlab.adapter import GitLabAdapter
from tests.contracts.source_control.conformance_suite import (
    assert_repository_operations,
    assert_webhook_parsing,
)


@pytest.fixture
def gitlab_connection() -> Connection:
    return Connection(
        name="test-gitlab",
        provider=Provider.GITLAB,
        base_url="https://gitlab.com/api/v4",
        credential_env="GITLAB_TOKEN",
        webhook_secret_env="GITLAB_WEBHOOK_SECRET",
    )


@pytest.fixture
def gitlab_repo_ref() -> RepositoryRef:
    return RepositoryRef(
        id="test/repo",
        provider=Provider.GITLAB,
        connection="test-gitlab",
        namespace="test/repo",
        default_branch="main",
        change_request_mode="fork",
    )


@pytest.fixture
def mock_gitlab_http_client() -> GitLabClient:
    client = GitLabClient(credential="test-token-123")
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    return client


@pytest.fixture
def gitlab_adapter_with_mock_client(
    gitlab_connection: Connection, mock_gitlab_http_client: GitLabClient
) -> GitLabAdapter:
    return GitLabAdapter(
        gitlab_connection, credential="test-token-123", client=mock_gitlab_http_client
    )


class TestResolveDefaultBranchAndIdentity:
    @pytest.mark.asyncio
    async def test_conformance_repository_operations(
        self,
        gitlab_adapter_with_mock_client: GitLabAdapter,
        gitlab_repo_ref: RepositoryRef,
        mock_gitlab_http_client: GitLabClient,
    ):
        mock_client = mock_gitlab_http_client._client

        def _get(path, **_kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if path.endswith("/user"):
                response.json.return_value = {"username": "forge-bot"}
            else:
                response.json.return_value = {"default_branch": "develop"}
            return response

        mock_client.get = AsyncMock(side_effect=_get)

        await assert_repository_operations(gitlab_adapter_with_mock_client, gitlab_repo_ref)


class TestGetGitCredentials:
    @pytest.mark.asyncio
    async def test_uses_oauth2_url_user(self, gitlab_adapter_with_mock_client, gitlab_repo_ref):
        credentials = await gitlab_adapter_with_mock_client.get_git_credentials(gitlab_repo_ref)
        assert credentials.host == "gitlab.com"
        assert credentials.token == "test-token-123"
        assert credentials.url_user == "oauth2"


class MockResolver:
    def __init__(self, repo_ref, connection):
        self._repo_ref = repo_ref
        self._connection = connection

    def resolve(
        self,
        identifier,  # noqa: ARG002
        provider_hint=None,  # noqa: ARG002
    ):
        return ResolvedRepository(
            repo_ref=self._repo_ref, connection=self._connection, adapter=None
        )


@pytest.fixture
def webhook_secret() -> str:
    return "test-webhook-secret"


class TestVerifyWebhook:
    @pytest.mark.asyncio
    async def test_valid_token_verifies(self, gitlab_connection, webhook_secret):
        adapter = GitLabAdapter(gitlab_connection, credential="tok", webhook_secret=webhook_secret)
        assert await adapter.verify_webhook({"X-Gitlab-Token": webhook_secret}, b"{}") is True

    @pytest.mark.asyncio
    async def test_invalid_token_fails(self, gitlab_connection, webhook_secret):
        adapter = GitLabAdapter(gitlab_connection, credential="tok", webhook_secret=webhook_secret)
        assert await adapter.verify_webhook({"X-Gitlab-Token": "wrong"}, b"{}") is False

    @pytest.mark.asyncio
    async def test_fails_closed_when_no_secret_configured(
        self, gitlab_adapter_with_mock_client, webhook_secret
    ):
        assert gitlab_adapter_with_mock_client._webhook_secret is None
        assert (
            await gitlab_adapter_with_mock_client.verify_webhook(
                {"X-Gitlab-Token": webhook_secret}, b"{}"
            )
            is False
        )


def _mr_opened_payload(**overrides) -> dict:
    payload = {
        "object_kind": "merge_request",
        "user": {"username": "alice", "name": "Alice"},
        "project": {"path_with_namespace": "test/repo"},
        "object_attributes": {
            "iid": 42,
            "title": "Test MR",
            "description": "Test body",
            "state": "opened",
            "action": "open",
            "source_branch": "feature",
            "target_branch": "main",
            "url": "https://gitlab.com/test/repo/-/merge_requests/42",
            "draft": False,
            "last_commit": {"id": "abc123"},
        },
    }
    payload.update(overrides)
    return payload


class TestParseWebhookMergeRequest:
    @pytest.mark.asyncio
    async def test_parses_mr_opened(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        body = json.dumps(_mr_opened_payload()).encode()
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        await assert_webhook_parsing(
            gitlab_adapter_with_mock_client,
            {"X-Gitlab-Event": "Merge Request Hook"},
            body,
            resolver,
            expected_kind=EventKind.CR_OPENED,
            expected_repo_namespace="test/repo",
        )

    @pytest.mark.asyncio
    async def test_maps_change_request_fields(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        body = json.dumps(_mr_opened_payload()).encode()
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {"X-Gitlab-Event": "Merge Request Hook"}, body, resolver
        )

        assert event.change_request.identity.native_id == 42
        assert event.change_request.title == "Test MR"
        assert event.change_request.body == "Test body"
        assert event.change_request.state == ChangeRequestState.OPEN
        assert event.change_request.head_sha == "abc123"
        assert event.actor.login == "alice"
        assert event.actor.is_bot is False

    @pytest.mark.asyncio
    async def test_synthesizes_event_id_from_body(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        """GitLab sends no delivery-id header; parse_webhook must synthesize one."""
        body = json.dumps(_mr_opened_payload()).encode()
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook({}, body, resolver)

        import hashlib

        assert event.id == hashlib.sha256(body).hexdigest()[:16]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "expected_kind"),
        [
            ("reopen", EventKind.CR_UPDATED),
            ("update", EventKind.CR_UPDATED),
            ("close", EventKind.CR_CLOSED),
            ("merge", EventKind.CR_MERGED),
            ("approved", EventKind.REVIEW_SUBMITTED),
            ("unapproved", EventKind.REVIEW_SUBMITTED),
        ],
    )
    async def test_maps_action_to_event_kind(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        gitlab_connection,
        action,
        expected_kind,
    ):
        payload = _mr_opened_payload()
        payload["object_attributes"]["action"] = action
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.kind == expected_kind

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "expected_state"),
        [
            ("approved", ReviewState.APPROVED),
            ("approval", ReviewState.APPROVED),
            ("unapproved", ReviewState.DISMISSED),
            ("unapproval", ReviewState.DISMISSED),
        ],
    )
    async def test_approval_actions_populate_review(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        gitlab_connection,
        action,
        expected_state,
    ):
        """worker.py's human-review-gate approval path and the PRD/spec
        proposal review paths require event.review to be populated to unpause;
        approving/unapproving a GitLab MR must not be a no-op for those gates."""
        payload = _mr_opened_payload()
        payload["object_attributes"]["action"] = action
        payload["user"] = {"username": "reviewer-bob"}
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.review is not None
        assert event.review.state == expected_state
        assert event.review.author == "reviewer-bob"
        assert event.review.comments == []

    @pytest.mark.asyncio
    async def test_non_approval_actions_leave_review_none(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        payload = _mr_opened_payload()
        payload["object_attributes"]["action"] = "update"
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.review is None


def test_map_change_request_rejects_both_repo_ref_and_identity(
    gitlab_adapter_with_mock_client: GitLabAdapter,
    gitlab_repo_ref: RepositoryRef,
):
    """_map_change_request's contract is "exactly one of repo_ref or identity" --
    passing both must raise rather than silently letting identity win."""
    identity = ChangeRequestIdentity(
        connection="test-gitlab", repository_id="test/repo", native_id=1
    )

    with pytest.raises(ValueError, match="not both"):
        gitlab_adapter_with_mock_client._map_change_request(
            _mr_opened_payload()["object_attributes"], repo_ref=gitlab_repo_ref, identity=identity
        )


def test_map_change_request_rejects_neither_repo_ref_nor_identity(
    gitlab_adapter_with_mock_client: GitLabAdapter,
):
    """Passing neither repo_ref nor identity must raise rather than blowing up
    with an opaque AttributeError when the missing repo_ref is dereferenced."""
    with pytest.raises(ValueError, match="requires either"):
        gitlab_adapter_with_mock_client._map_change_request(
            _mr_opened_payload()["object_attributes"]
        )


class TestParseWebhookNote:
    @pytest.mark.asyncio
    async def test_note_on_merge_request_is_comment_created(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        payload = {
            "object_kind": "note",
            "user": {"username": "bob"},
            "project": {"path_with_namespace": "test/repo"},
            "object_attributes": {
                "id": 555,
                "note": "Looks good",
                "noteable_type": "MergeRequest",
            },
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.kind == EventKind.COMMENT_CREATED
        assert event.comment.id == "555"
        assert event.comment.body == "Looks good"
        assert event.comment.author == "bob"
        assert event.comment.in_reply_to is None

    @pytest.mark.asyncio
    async def test_note_on_merge_request_populates_change_request(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        """A note-webhook payload carries a top-level `merge_request` object;
        without change_request populated from it, `_extract_ticket_key` in the
        gitlab route has nothing to fall back to (note events have no
        top-level `ref`) and worker.py drops the event before it reaches
        any workflow gate."""
        payload = {
            "object_kind": "note",
            "user": {"username": "bob"},
            "project": {"path_with_namespace": "test/repo"},
            "object_attributes": {
                "id": 555,
                "note": "!skip-gate flaky-test",
                "noteable_type": "MergeRequest",
            },
            "merge_request": {
                "iid": 42,
                "title": "AISOS-1: Test MR",
                "description": "Test body",
                "state": "opened",
                "source_branch": "forge/AISOS-1",
                "target_branch": "main",
                "url": "https://gitlab.com/test/repo/-/merge_requests/42",
                "last_commit": {"id": "def456"},
                "draft": False,
            },
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.change_request is not None
        assert event.change_request.identity.native_id == 42
        assert event.change_request.title == "AISOS-1: Test MR"
        assert event.change_request.source_branch == "forge/AISOS-1"
        assert event.change_request.target_branch == "main"
        assert event.change_request.state == ChangeRequestState.OPEN
        assert event.change_request.head_sha == "def456"

    @pytest.mark.asyncio
    async def test_note_on_merge_request_without_mr_object_leaves_change_request_none(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        """Not every note payload is guaranteed to carry `merge_request` (e.g.
        a malformed or unusual payload); parse_webhook must degrade gracefully
        rather than raising."""
        payload = {
            "object_kind": "note",
            "user": {"username": "bob"},
            "project": {"path_with_namespace": "test/repo"},
            "object_attributes": {
                "id": 555,
                "note": "Looks good",
                "noteable_type": "MergeRequest",
            },
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.change_request is None
        assert event.comment is not None

    @pytest.mark.asyncio
    async def test_note_on_issue_is_unknown(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        payload = {
            "object_kind": "note",
            "user": {"username": "bob"},
            "project": {"path_with_namespace": "test/repo"},
            "object_attributes": {"id": 1, "note": "x", "noteable_type": "Issue"},
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.kind == EventKind.UNKNOWN


class TestParseWebhookPipeline:
    @pytest.mark.asyncio
    async def test_pipeline_sets_check_suite_status_not_check(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        from forge.integrations.source_control.contracts import CheckStatus

        payload = {
            "object_kind": "pipeline",
            "user": {"username": "ci-bot"},
            "project": {"path_with_namespace": "test/repo"},
            "object_attributes": {"id": 999, "status": "running", "ref": "feature"},
            "merge_request": {
                "iid": 42,
                "source_branch": "feature",
                "target_branch": "main",
                "state": "opened",
            },
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.kind == EventKind.CHECK_UPDATED
        assert event.check is None
        assert event.check_suite_status == CheckStatus.IN_PROGRESS
        assert event.change_request.identity.native_id == 42


class TestParseWebhookPush:
    @pytest.mark.asyncio
    async def test_push_reads_top_level_user_fields(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, gitlab_connection
    ):
        payload = {
            "object_kind": "push",
            "user_username": "carol",
            "project": {"path_with_namespace": "test/repo"},
        }
        resolver = MockResolver(gitlab_repo_ref, gitlab_connection)

        event = await gitlab_adapter_with_mock_client.parse_webhook(
            {}, json.dumps(payload).encode(), resolver
        )

        assert event.kind == EventKind.PUSH
        assert event.actor.login == "carol"
        assert event.change_request is None


class TestTranslateProviderErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "expected_exception"),
        [(401, AuthenticationError), (429, RateLimitedError), (503, TransientProviderError)],
    )
    async def test_status_code_maps_to_neutral_exception(
        self,
        gitlab_adapter_with_mock_client: GitLabAdapter,
        gitlab_repo_ref: RepositoryRef,
        mock_gitlab_http_client: GitLabClient,
        status_code: int,
        expected_exception: type[Exception],
    ):
        response = httpx.Response(
            status_code,
            headers={"Retry-After": "5"} if status_code == 429 else {},
            request=httpx.Request("GET", "https://gitlab.com/api/v4/projects/test%2Frepo"),
        )
        mock_gitlab_http_client.get_project = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )
        with pytest.raises(expected_exception):
            await gitlab_adapter_with_mock_client.resolve_default_branch(gitlab_repo_ref)


class TestEnsureWriteTarget:
    @pytest.mark.asyncio
    async def test_direct_mode_makes_no_api_calls(
        self, gitlab_adapter_with_mock_client, mock_gitlab_http_client
    ):
        direct_ref = RepositoryRef(
            id="test/repo",
            provider=Provider.GITLAB,
            connection="test-gitlab",
            namespace="test/repo",
            default_branch="main",
            change_request_mode="direct",
        )

        target = await gitlab_adapter_with_mock_client.ensure_write_target(direct_ref)

        assert target.clone_url == "https://gitlab.com/test/repo.git"
        assert target.push_remote_name == "origin"
        assert target.base_branch == "main"
        mock_gitlab_http_client._client.get.assert_not_called()
        mock_gitlab_http_client._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_fork_mode_creates_and_returns_fork_target(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_authenticated_user = AsyncMock(
            return_value={"username": "forge-bot"}
        )
        mock_gitlab_http_client.get_or_create_fork = AsyncMock(
            return_value={
                "id": 7,
                "path_with_namespace": "forge-bot/repo",
                "http_url_to_repo": "https://gitlab.com/forge-bot/repo.git",
            }
        )

        target = await gitlab_adapter_with_mock_client.ensure_write_target(gitlab_repo_ref)

        mock_gitlab_http_client.get_or_create_fork.assert_awaited_once_with(
            "test/repo", fork_owner="forge-bot"
        )
        assert target.clone_url == "https://gitlab.com/forge-bot/repo.git"
        assert target.fork_owner == "forge-bot"
        assert target.fork_repo == "repo"


class TestCreateChangeRequest:
    @pytest.mark.asyncio
    async def test_direct_mode_creates_mr_without_target_project_id(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        write_target = WriteTarget(
            clone_url="https://gitlab.com/test/repo.git",
            push_remote_name="origin",
            head_ref="forge/test/repo",
            base_branch="main",
        )
        mock_gitlab_http_client.create_merge_request = AsyncMock(
            return_value={
                "iid": 5,
                "title": "Test MR",
                "description": "body",
                "state": "opened",
                "source_branch": "forge/test/repo",
                "target_branch": "main",
                "web_url": "https://gitlab.com/test/repo/-/merge_requests/5",
                "draft": False,
            }
        )

        cr = await gitlab_adapter_with_mock_client.create_change_request(
            gitlab_repo_ref, write_target, title="Test MR", body="body"
        )

        mock_gitlab_http_client.create_merge_request.assert_awaited_once_with(
            "test/repo",
            source_branch="forge/test/repo",
            target_branch="main",
            title="Test MR",
            description="body",
            target_project_id=None,
        )
        assert cr.identity.native_id == 5
        assert cr.url == "https://gitlab.com/test/repo/-/merge_requests/5"

    @pytest.mark.asyncio
    async def test_fork_mode_resolves_upstream_numeric_id_and_uses_fork_as_source(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        write_target = WriteTarget(
            clone_url="https://gitlab.com/forge-bot/repo.git",
            push_remote_name="origin",
            head_ref="forge/test/repo",
            base_branch="main",
            fork_owner="forge-bot",
            fork_repo="repo",
        )
        mock_gitlab_http_client.get_project = AsyncMock(return_value={"id": 100})
        mock_gitlab_http_client.create_merge_request = AsyncMock(
            return_value={
                "iid": 6,
                "title": "T",
                "description": "",
                "state": "opened",
                "source_branch": "forge/test/repo",
                "target_branch": "main",
                "web_url": "u",
                "draft": True,
            }
        )

        cr = await gitlab_adapter_with_mock_client.create_change_request(
            gitlab_repo_ref, write_target, title="T", body="", draft=True
        )

        mock_gitlab_http_client.get_project.assert_awaited_once_with("test/repo")
        mock_gitlab_http_client.create_merge_request.assert_awaited_once_with(
            "forge-bot/repo",
            source_branch="forge/test/repo",
            target_branch="main",
            title="Draft: T",
            description="",
            target_project_id=100,
        )
        assert cr.draft is True

    @pytest.mark.asyncio
    async def test_409_conflict_returns_existing_mr_uncreated(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """GitLab returns 409 (not GitHub's 422) when an open MR already
        exists for the source branch. This must not leak a raw
        httpx.HTTPStatusError past the adapter boundary -- the existing MR is
        looked up and returned with created=False, mirroring GitHub's
        create_pull_request behavior."""
        write_target = WriteTarget(
            clone_url="https://gitlab.com/test/repo.git",
            push_remote_name="origin",
            head_ref="forge/test/repo",
            base_branch="main",
        )
        conflict_response = httpx.Response(
            409,
            json={
                "message": ["Another open merge request already exists for this source branch: !7"]
            },
            request=httpx.Request(
                "POST", "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests"
            ),
        )
        mock_gitlab_http_client.create_merge_request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "409 conflict", request=conflict_response.request, response=conflict_response
            )
        )
        mock_gitlab_http_client.get_merge_requests = AsyncMock(
            return_value=[
                {
                    "iid": 7,
                    "title": "Existing MR",
                    "description": "already open",
                    "state": "opened",
                    "source_branch": "forge/test/repo",
                    "target_branch": "main",
                    "web_url": "https://gitlab.com/test/repo/-/merge_requests/7",
                    "draft": False,
                }
            ]
        )

        cr = await gitlab_adapter_with_mock_client.create_change_request(
            gitlab_repo_ref, write_target, title="Test MR", body="body"
        )

        mock_gitlab_http_client.get_merge_requests.assert_awaited_once_with(
            "test/repo", source_branch="forge/test/repo"
        )
        assert cr.identity.native_id == 7
        assert cr.created is False
        assert cr.url == "https://gitlab.com/test/repo/-/merge_requests/7"

    @pytest.mark.asyncio
    async def test_409_conflict_with_no_matching_mr_raises_conflict_error(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """If GitLab reports the conflict but the lookup finds nothing (e.g. a
        race where the MR was closed between the 409 and the lookup), this
        must still not leak a raw httpx type -- fall back to ConflictError."""
        write_target = WriteTarget(
            clone_url="https://gitlab.com/test/repo.git",
            push_remote_name="origin",
            head_ref="forge/test/repo",
            base_branch="main",
        )
        conflict_response = httpx.Response(
            409,
            json={"message": ["Another open merge request already exists"]},
            request=httpx.Request(
                "POST", "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests"
            ),
        )
        mock_gitlab_http_client.create_merge_request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "409 conflict", request=conflict_response.request, response=conflict_response
            )
        )
        mock_gitlab_http_client.get_merge_requests = AsyncMock(return_value=[])

        with pytest.raises(ConflictError, match="already exists"):
            await gitlab_adapter_with_mock_client.create_change_request(
                gitlab_repo_ref, write_target, title="Test MR", body="body"
            )

    @pytest.mark.asyncio
    async def test_non_409_http_error_propagates_through_translate(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """A non-409 HTTPStatusError must still reach @_translate's neutral
        mapping, not get swallowed by the 409-specific handling."""
        write_target = WriteTarget(
            clone_url="https://gitlab.com/test/repo.git",
            push_remote_name="origin",
            head_ref="forge/test/repo",
            base_branch="main",
        )
        error_response = httpx.Response(
            500,
            request=httpx.Request(
                "POST", "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests"
            ),
        )
        mock_gitlab_http_client.create_merge_request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "server error", request=error_response.request, response=error_response
            )
        )

        with pytest.raises(TransientProviderError):
            await gitlab_adapter_with_mock_client.create_change_request(
                gitlab_repo_ref, write_target, title="Test MR", body="body"
            )


class TestUpdateChangeRequest:
    @pytest.mark.asyncio
    async def test_maps_closed_state_to_state_event(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=5
        )
        mock_gitlab_http_client.update_merge_request = AsyncMock(
            return_value={
                "iid": 5,
                "title": "T",
                "description": "",
                "state": "closed",
                "source_branch": "f",
                "target_branch": "main",
            }
        )

        cr = await gitlab_adapter_with_mock_client.update_change_request(
            gitlab_repo_ref, identity, state=ChangeRequestState.CLOSED
        )

        assert (
            mock_gitlab_http_client.update_merge_request.await_args.kwargs["state_event"] == "close"
        )
        assert cr.state == ChangeRequestState.CLOSED

    @pytest.mark.asyncio
    async def test_rejects_merged_state(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=5
        )
        mock_gitlab_http_client.update_merge_request = AsyncMock()

        with pytest.raises(ValueError, match="MERGED"):
            await gitlab_adapter_with_mock_client.update_change_request(
                gitlab_repo_ref, identity, state=ChangeRequestState.MERGED
            )
        mock_gitlab_http_client.update_merge_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_on_missing_native_id(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=None
        )
        mock_gitlab_http_client.get_merge_request = AsyncMock()

        with pytest.raises(ValueError, match="native_id"):
            await gitlab_adapter_with_mock_client.get_change_request(gitlab_repo_ref, identity)
        mock_gitlab_http_client.get_merge_request.assert_not_awaited()


class TestCreateComment:
    @pytest.mark.asyncio
    async def test_creates_note_and_maps_result(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.create_note = AsyncMock(
            return_value={"id": 1, "body": "hi", "author": {"username": "forge-bot"}}
        )

        comment = await gitlab_adapter_with_mock_client.create_comment(
            gitlab_repo_ref, identity, "hi"
        )

        mock_gitlab_http_client.create_note.assert_awaited_once_with("test/repo", 42, "hi")
        assert comment.body == "hi"
        assert comment.author == "forge-bot"


class TestReplyToComment:
    @pytest.mark.asyncio
    async def test_finds_discussion_containing_note_and_replies(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(
            return_value=[
                {"id": "disc-1", "notes": [{"id": 10}]},
                {"id": "disc-2", "notes": [{"id": 20}, {"id": 21}]},
            ]
        )
        mock_gitlab_http_client.reply_to_discussion = AsyncMock(
            return_value={"id": 22, "body": "reply", "author": {"username": "forge-bot"}}
        )

        comment = await gitlab_adapter_with_mock_client.reply_to_comment(
            gitlab_repo_ref, identity, comment_id="20", body="reply"
        )

        mock_gitlab_http_client.reply_to_discussion.assert_awaited_once_with(
            "test/repo", 42, "disc-2", "reply"
        )
        assert comment.in_reply_to == "20"

    @pytest.mark.asyncio
    async def test_raises_not_found_when_no_discussion_contains_the_note(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(
            return_value=[{"id": "disc-1", "notes": [{"id": 10}]}]
        )
        mock_gitlab_http_client.reply_to_discussion = AsyncMock()

        with pytest.raises(SCNotFoundError):
            await gitlab_adapter_with_mock_client.reply_to_comment(
                gitlab_repo_ref, identity, comment_id="999", body="x"
            )
        mock_gitlab_http_client.reply_to_discussion.assert_not_awaited()


class TestGetReviewThreads:
    @pytest.mark.asyncio
    async def test_maps_approvals_to_approved_reviews(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_approvals = AsyncMock(
            return_value={"approved_by": [{"user": {"id": 1, "username": "alice"}}]}
        )

        reviews = await gitlab_adapter_with_mock_client.get_review_threads(
            gitlab_repo_ref, identity
        )

        assert len(reviews) == 1
        assert reviews[0].state == ReviewState.APPROVED
        assert reviews[0].author == "alice"
        assert reviews[0].comments == []

    @pytest.mark.asyncio
    async def test_no_approvals_returns_empty_list(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_approvals = AsyncMock(return_value={"approved_by": []})

        reviews = await gitlab_adapter_with_mock_client.get_review_threads(
            gitlab_repo_ref, identity
        )

        assert reviews == []

    @pytest.mark.asyncio
    async def test_degrades_to_empty_list_on_confirmed_404_and_caches(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        approvals_response = httpx.Response(
            404,
            request=httpx.Request(
                "GET",
                "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests/42/approvals",
            ),
        )
        mock_gitlab_http_client.get_approvals = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not found", request=approvals_response.request, response=approvals_response
            )
        )
        mock_gitlab_http_client.get_merge_request = AsyncMock(return_value={"iid": 42})

        reviews = await gitlab_adapter_with_mock_client.get_review_threads(
            gitlab_repo_ref, identity
        )
        assert reviews == []
        mock_gitlab_http_client.get_merge_request.assert_awaited_once_with("test/repo", 42)

        mock_gitlab_http_client.get_approvals.reset_mock()
        reviews_again = await gitlab_adapter_with_mock_client.get_review_threads(
            gitlab_repo_ref, identity
        )
        assert reviews_again == []
        mock_gitlab_http_client.get_approvals.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_mr_404_still_raises_and_does_not_poison_other_mrs(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity_missing = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=1
        )
        identity_real = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=2
        )

        approvals_response = httpx.Response(
            404,
            request=httpx.Request(
                "GET",
                "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests/1/approvals",
            ),
        )
        mr_response = httpx.Response(
            404,
            request=httpx.Request(
                "GET", "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests/1"
            ),
        )
        mock_gitlab_http_client.get_approvals = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not found", request=approvals_response.request, response=approvals_response
            )
        )
        mock_gitlab_http_client.get_merge_request = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not found", request=mr_response.request, response=mr_response
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            await gitlab_adapter_with_mock_client.get_review_threads(
                gitlab_repo_ref, identity_missing
            )

        mock_gitlab_http_client.get_approvals = AsyncMock(
            return_value={"approved_by": [{"user": {"id": 9, "username": "bob"}}]}
        )
        reviews = await gitlab_adapter_with_mock_client.get_review_threads(
            gitlab_repo_ref, identity_real
        )
        assert len(reviews) == 1
        assert reviews[0].author == "bob"

    @pytest.mark.asyncio
    async def test_non_404_error_on_approvals_still_raises(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        error_response = httpx.Response(
            500,
            request=httpx.Request(
                "GET",
                "https://gitlab.com/api/v4/projects/test%2Frepo/merge_requests/42/approvals",
            ),
        )
        mock_gitlab_http_client.get_approvals = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "server error", request=error_response.request, response=error_response
            )
        )

        with pytest.raises(TransientProviderError):
            await gitlab_adapter_with_mock_client.get_review_threads(gitlab_repo_ref, identity)

    @pytest.mark.asyncio
    async def test_raises_on_missing_native_id_even_after_approvals_confirmed_unsupported(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """The _approvals_supported=False short-circuit must not bypass
        native_id validation -- a malformed identity should still raise,
        not silently return [] once the capability cache is warmed."""
        gitlab_adapter_with_mock_client._approvals_supported = False
        mock_gitlab_http_client.get_approvals = AsyncMock()
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=None
        )

        with pytest.raises(ValueError, match="native_id"):
            await gitlab_adapter_with_mock_client.get_review_threads(gitlab_repo_ref, identity)
        mock_gitlab_http_client.get_approvals.assert_not_awaited()


class TestGetReviewThreadComments:
    @pytest.mark.asyncio
    async def test_filters_to_unresolved_diff_notes(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(
            return_value=[
                {
                    "id": "disc-1",
                    "notes": [
                        {
                            "id": 1,
                            "body": "fix this",
                            "author": {"username": "bob"},
                            "type": "DiffNote",
                            "resolvable": True,
                            "resolved": False,
                            "position": {"new_path": "src/x.py", "new_line": 10},
                        }
                    ],
                },
                {
                    "id": "disc-2",
                    "notes": [
                        {
                            "id": 2,
                            "body": "done",
                            "author": {"username": "carol"},
                            "type": "DiffNote",
                            "resolvable": True,
                            "resolved": True,
                        }
                    ],
                },
                {
                    "id": "disc-3",
                    "notes": [
                        {
                            "id": 3,
                            "body": "general comment",
                            "author": {"username": "dave"},
                            "type": None,
                        }
                    ],
                },
            ]
        )

        reviews = await gitlab_adapter_with_mock_client.get_review_thread_comments(
            gitlab_repo_ref, identity
        )

        assert [r.id for r in reviews] == ["disc-1"]
        assert reviews[0].comments[0].body == "fix this"
        assert reviews[0].comments[0].path == "src/x.py"
        assert reviews[0].comments[0].line == 10


class TestGetReviewCommentsForSubmission:
    @pytest.mark.asyncio
    async def test_returns_matching_discussion_notes(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(
            return_value=[
                {"id": "disc-1", "notes": [{"id": 1, "body": "x", "author": {"username": "bob"}}]}
            ]
        )

        comments = await gitlab_adapter_with_mock_client.get_review_comments_for_submission(
            gitlab_repo_ref, identity, review_id="disc-1"
        )

        assert len(comments) == 1
        assert comments[0].body == "x"

    @pytest.mark.asyncio
    async def test_resolved_note_maps_resolved_true(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """_map_note_response must surface GitLab's `resolved` field rather
        than leaving ReviewComment.resolved at its dataclass default (False)
        for every note, resolved or not."""
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(
            return_value=[
                {
                    "id": "disc-1",
                    "notes": [
                        {
                            "id": 1,
                            "body": "fixed now",
                            "author": {"username": "bob"},
                            "resolved": True,
                        }
                    ],
                }
            ]
        )

        comments = await gitlab_adapter_with_mock_client.get_review_comments_for_submission(
            gitlab_repo_ref, identity, review_id="disc-1"
        )

        assert len(comments) == 1
        assert comments[0].resolved is True

    @pytest.mark.asyncio
    async def test_unmatched_id_returns_empty_list_not_error(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=42
        )
        mock_gitlab_http_client.get_discussions = AsyncMock(return_value=[])

        comments = await gitlab_adapter_with_mock_client.get_review_comments_for_submission(
            gitlab_repo_ref, identity, review_id="does-not-exist"
        )

        assert comments == []


class TestGetChecks:
    @pytest.mark.asyncio
    async def test_maps_status_and_parses_job_id_from_target_url(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[
                {
                    "name": "build",
                    "status": "success",
                    "target_url": "https://gitlab.com/test/repo/-/jobs/987654",
                }
            ]
        )

        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")

        mock_gitlab_http_client.get_commit_statuses.assert_awaited_once_with("test/repo", "abc123")
        assert checks[0].status == CheckStatus.COMPLETED
        assert checks[0].conclusion == CheckConclusion.SUCCESS
        assert checks[0].logs_url == "987654"

    @pytest.mark.asyncio
    async def test_external_ci_target_url_has_no_logs_url(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[
                {
                    "name": "jenkins",
                    "status": "success",
                    "target_url": "https://ci.example.com/build/1",
                }
            ]
        )

        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")

        assert checks[0].logs_url is None

    @pytest.mark.asyncio
    async def test_manual_status_maps_to_queued_none(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[{"name": "deploy", "status": "manual", "target_url": None}]
        )

        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")

        assert checks[0].status == CheckStatus.QUEUED
        assert checks[0].conclusion == CheckConclusion.NONE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("gitlab_status", "expected_status", "expected_conclusion"),
        [
            ("failed", CheckStatus.COMPLETED, CheckConclusion.FAILURE),
            ("canceled", CheckStatus.COMPLETED, CheckConclusion.CANCELLED),
            ("skipped", CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
        ],
    )
    async def test_maps_terminal_statuses(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        mock_gitlab_http_client,
        gitlab_status,
        expected_status,
        expected_conclusion,
    ):
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[{"name": "build", "status": gitlab_status, "target_url": None}]
        )

        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")

        assert checks[0].status == expected_status
        assert checks[0].conclusion == expected_conclusion

    @pytest.mark.asyncio
    async def test_fork_mode_queries_source_project_not_upstream(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """A fork-mode MR runs its pipeline in the source (fork) project, not
        repo_ref.namespace (the upstream/target project) -- querying the
        upstream for a fork commit's statuses returns nothing and CI
        evaluation never progresses. get_change_request must have primed the
        source-project cache (via _map_change_request) before get_checks is
        called, mirroring how ci_evaluator always calls them in sequence."""
        mock_gitlab_http_client.get_merge_request = AsyncMock(
            return_value={
                "iid": 5,
                "title": "T",
                "description": "",
                "state": "opened",
                "source_branch": "forge/test/repo",
                "target_branch": "main",
                "web_url": "u",
                "source_project_id": 200,
                "target_project_id": 100,
                "last_commit": {"id": "abc123"},
            }
        )
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=5
        )
        await gitlab_adapter_with_mock_client.get_change_request(gitlab_repo_ref, identity)

        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[
                {
                    "name": "build",
                    "status": "success",
                    "target_url": "https://gitlab.com/forge-bot/repo/-/jobs/987654",
                }
            ]
        )

        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")

        mock_gitlab_http_client.get_commit_statuses.assert_awaited_once_with("200", "abc123")
        assert checks[0].logs_url == "987654"

    @pytest.mark.asyncio
    async def test_same_project_mr_queries_upstream(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """Direct-mode (non-fork) MRs have matching source/target project ids
        -- must keep querying repo_ref.namespace, not start using the numeric
        project id for every MR."""
        mock_gitlab_http_client.get_merge_request = AsyncMock(
            return_value={
                "iid": 5,
                "title": "T",
                "description": "",
                "state": "opened",
                "source_branch": "forge/test/repo",
                "target_branch": "main",
                "web_url": "u",
                "source_project_id": 100,
                "target_project_id": 100,
                "last_commit": {"id": "def456"},
            }
        )
        identity = ChangeRequestIdentity(
            connection="test-gitlab", repository_id="test/repo", native_id=5
        )
        await gitlab_adapter_with_mock_client.get_change_request(gitlab_repo_ref, identity)

        mock_gitlab_http_client.get_commit_statuses = AsyncMock(return_value=[])

        await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "def456")

        mock_gitlab_http_client.get_commit_statuses.assert_awaited_once_with("test/repo", "def456")


class TestGetCheckLogs:
    @pytest.mark.asyncio
    async def test_fetches_trace_directly_by_job_id(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_job_trace = AsyncMock(return_value="log output")
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        logs = await gitlab_adapter_with_mock_client.get_check_logs(gitlab_repo_ref, check)

        mock_gitlab_http_client.get_job_trace.assert_awaited_once_with("test/repo", 987654)
        assert logs == "log output"

    @pytest.mark.asyncio
    async def test_raises_not_found_when_no_logs_url(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_job_trace = AsyncMock()
        check = CheckRun(
            name="jenkins",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url=None,
        )

        with pytest.raises(SCNotFoundError, match="No logs available"):
            await gitlab_adapter_with_mock_client.get_check_logs(gitlab_repo_ref, check)
        mock_gitlab_http_client.get_job_trace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_source_control_error_for_non_numeric_logs_url(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        mock_gitlab_http_client,  # noqa: ARG002
    ):
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="not-a-number",
        )

        with pytest.raises(SourceControlError, match="non-numeric logs_url"):
            await gitlab_adapter_with_mock_client.get_check_logs(gitlab_repo_ref, check)

    @pytest.mark.asyncio
    async def test_raises_not_found_when_trace_returns_404(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        response = httpx.Response(
            404,
            request=httpx.Request(
                "GET", "https://gitlab.com/api/v4/projects/test%2Frepo/jobs/987654/trace"
            ),
        )
        mock_gitlab_http_client.get_job_trace = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        with pytest.raises(SCNotFoundError):
            await gitlab_adapter_with_mock_client.get_check_logs(gitlab_repo_ref, check)

    @pytest.mark.asyncio
    async def test_fork_mode_fetches_trace_from_source_project(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        """The job id in logs_url only exists in the fork project that ran
        it -- get_checks must have recorded which project each job id came
        from (see get_checks) so get_check_logs queries that project rather
        than the upstream, where the job doesn't exist."""
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[
                {
                    "name": "build",
                    "status": "success",
                    "target_url": "https://gitlab.com/forge-bot/repo/-/jobs/987654",
                }
            ]
        )
        gitlab_adapter_with_mock_client._source_project_by_ref["abc123"] = "200"
        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")
        mock_gitlab_http_client.get_job_trace = AsyncMock(return_value="log output")

        logs = await gitlab_adapter_with_mock_client.get_check_logs(gitlab_repo_ref, checks[0])

        mock_gitlab_http_client.get_job_trace.assert_awaited_once_with("200", 987654)
        assert logs == "log output"


class TestGetCheckArtifacts:
    @pytest.mark.asyncio
    async def test_returns_single_zip_entry(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_job_artifacts = AsyncMock(return_value=b"zipbytes")
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        artifacts = await gitlab_adapter_with_mock_client.get_check_artifacts(
            gitlab_repo_ref, check
        )

        assert artifacts == [("artifacts.zip", b"zipbytes")]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_logs_url(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        mock_gitlab_http_client,  # noqa: ARG002
    ):
        check = CheckRun(
            name="jenkins",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url=None,
        )

        assert (
            await gitlab_adapter_with_mock_client.get_check_artifacts(gitlab_repo_ref, check) == []
        )

    @pytest.mark.asyncio
    async def test_raises_source_control_error_for_non_numeric_logs_url(
        self,
        gitlab_adapter_with_mock_client,
        gitlab_repo_ref,
        mock_gitlab_http_client,  # noqa: ARG002
    ):
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="not-a-number",
        )

        with pytest.raises(SourceControlError, match="non-numeric logs_url"):
            await gitlab_adapter_with_mock_client.get_check_artifacts(gitlab_repo_ref, check)

    @pytest.mark.asyncio
    async def test_fork_mode_fetches_artifacts_from_source_project(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_commit_statuses = AsyncMock(
            return_value=[
                {
                    "name": "build",
                    "status": "success",
                    "target_url": "https://gitlab.com/forge-bot/repo/-/jobs/987654",
                }
            ]
        )
        gitlab_adapter_with_mock_client._source_project_by_ref["abc123"] = "200"
        checks = await gitlab_adapter_with_mock_client.get_checks(gitlab_repo_ref, "abc123")
        mock_gitlab_http_client.get_job_artifacts = AsyncMock(return_value=b"zipbytes")

        artifacts = await gitlab_adapter_with_mock_client.get_check_artifacts(
            gitlab_repo_ref, checks[0]
        )

        mock_gitlab_http_client.get_job_artifacts.assert_awaited_once_with("200", 987654)
        assert artifacts == [("artifacts.zip", b"zipbytes")]


class TestGetFile:
    @pytest.mark.asyncio
    async def test_returns_raw_content(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_raw = AsyncMock(return_value="print('hi')\n")

        content = await gitlab_adapter_with_mock_client.get_file(
            gitlab_repo_ref, "src/x.py", "main"
        )

        mock_gitlab_http_client.get_file_raw.assert_awaited_once_with(
            "test/repo", "src/x.py", "main"
        )
        assert content == "print('hi')\n"

    @pytest.mark.asyncio
    async def test_raises_not_found_when_missing(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_raw = AsyncMock(return_value=None)

        with pytest.raises(SCNotFoundError):
            await gitlab_adapter_with_mock_client.get_file(gitlab_repo_ref, "missing.py", "main")


class TestPutFile:
    @pytest.mark.asyncio
    async def test_updates_existing_file_with_last_commit_id(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_metadata = AsyncMock(
            return_value={"last_commit_id": "deadbeef"}
        )
        mock_gitlab_http_client.update_file = AsyncMock()
        mock_gitlab_http_client.create_file = AsyncMock()

        await gitlab_adapter_with_mock_client.put_file(
            gitlab_repo_ref, "src/x.py", "new content", "update x", "main"
        )

        mock_gitlab_http_client.update_file.assert_awaited_once_with(
            "test/repo",
            "src/x.py",
            branch="main",
            content="new content",
            commit_message="update x",
            last_commit_id="deadbeef",
        )
        mock_gitlab_http_client.create_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_new_file_when_absent(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_metadata = AsyncMock(return_value=None)
        mock_gitlab_http_client.create_file = AsyncMock()
        mock_gitlab_http_client.update_file = AsyncMock()

        await gitlab_adapter_with_mock_client.put_file(
            gitlab_repo_ref, "new.py", "content", "add new", "main"
        )

        mock_gitlab_http_client.create_file.assert_awaited_once_with(
            "test/repo", "new.py", branch="main", content="content", commit_message="add new"
        )
        mock_gitlab_http_client.update_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_commit_id_translates_to_conflict_error(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_metadata = AsyncMock(
            return_value={"last_commit_id": "stale"}
        )
        response = httpx.Response(
            400,
            json={
                "message": "You are attempting to update a file that has changed since you started editing it"
            },
            request=httpx.Request("PUT", "https://gitlab.com/api/v4/x"),
        )
        mock_gitlab_http_client.update_file = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(ConflictError):
            await gitlab_adapter_with_mock_client.put_file(
                gitlab_repo_ref, "src/x.py", "content", "update", "main"
            )

    @pytest.mark.asyncio
    async def test_unrelated_400_propagates_unchanged(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.get_file_metadata = AsyncMock(return_value={"last_commit_id": "x"})
        response = httpx.Response(
            400,
            json={"message": "Path is invalid"},
            request=httpx.Request("PUT", "https://gitlab.com/api/v4/x"),
        )
        mock_gitlab_http_client.update_file = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(httpx.HTTPStatusError):
            await gitlab_adapter_with_mock_client.put_file(
                gitlab_repo_ref, "src/x.py", "content", "update", "main"
            )


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_creates_branch(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        mock_gitlab_http_client.create_branch = AsyncMock()

        await gitlab_adapter_with_mock_client.create_branch(gitlab_repo_ref, "feature", "main")

        mock_gitlab_http_client.create_branch.assert_awaited_once_with(
            "test/repo", "feature", "main"
        )

    @pytest.mark.asyncio
    async def test_already_exists_is_swallowed_as_idempotent(
        self, gitlab_adapter_with_mock_client, gitlab_repo_ref, mock_gitlab_http_client
    ):
        response = httpx.Response(
            400,
            json={"message": "Branch already exists"},
            request=httpx.Request("POST", "https://gitlab.com/api/v4/x"),
        )
        mock_gitlab_http_client.create_branch = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        await gitlab_adapter_with_mock_client.create_branch(
            gitlab_repo_ref, "feature", "main"
        )  # must not raise


def test_gitlab_adapter_satisfies_source_control_provider_protocol(gitlab_adapter_with_mock_client):
    assert isinstance(gitlab_adapter_with_mock_client, SourceControlProvider)
