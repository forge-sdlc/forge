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
    WriteTarget,
)
from forge.integrations.source_control.errors import (
    AuthenticationError,
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
