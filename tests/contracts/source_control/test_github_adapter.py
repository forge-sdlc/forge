"""Tests for GitHub source control adapter."""

import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.integrations.github.client import GitHubClient, PullRequestCreationResult
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
    ConflictError,
    NotFoundError,
    RateLimitedError,
    SourceControlError,
    TransientProviderError,
)
from forge.integrations.source_control.github.adapter import GitHubAdapter
from tests.contracts.source_control.conformance_suite import (
    assert_repository_operations,
    assert_webhook_parsing,
    assert_webhook_verification,
)


@pytest.fixture
def github_connection() -> Connection:
    return Connection(
        name="test-github",
        provider=Provider.GITHUB,
        base_url="https://api.github.com",
        credential_env="GITHUB_TOKEN",
        webhook_secret_env="GITHUB_WEBHOOK_SECRET",
    )


@pytest.fixture
def github_repo_ref() -> RepositoryRef:
    return RepositoryRef(
        id="test/repo",
        provider=Provider.GITHUB,
        connection="test-github",
        namespace="test/repo",
        default_branch="main",
        change_request_mode="fork",
    )


@pytest.fixture
def github_adapter(github_connection: Connection) -> GitHubAdapter:
    return GitHubAdapter(github_connection, credential="test-token-123")


@pytest.fixture
def webhook_secret() -> str:
    return "test-webhook-secret"


@pytest.fixture
def mock_github_http_client(mock_settings) -> GitHubClient:
    """A GitHubClient whose underlying httpx.AsyncClient is mocked out."""
    client = GitHubClient(settings=mock_settings)
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    return client


@pytest.fixture
def github_adapter_with_mock_client(
    github_connection: Connection, mock_github_http_client: GitHubClient
) -> GitHubAdapter:
    """GitHubAdapter wired up with a mocked GitHubClient for HTTP-free tests."""
    return GitHubAdapter(
        github_connection,
        credential="test-token-123",
        client=mock_github_http_client,
    )


@pytest.fixture
def valid_pr_opened_payload() -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "html_url": "https://github.com/test/repo/pull/42",
            "title": "Test PR",
            "body": "Test body",
            "state": "open",
            "draft": False,
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
        },
        "repository": {
            "full_name": "test/repo",
        },
        "sender": {
            "login": "testuser",
            "type": "User",
        },
    }


def sign_webhook(payload: dict, secret: str) -> str:
    """Create GitHub webhook signature."""
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={signature}"


class MockResolver:
    """Mock repository resolver for testing."""

    def __init__(self, repo_ref: RepositoryRef, connection: Connection):
        self._repo_ref = repo_ref
        self._connection = connection

    def resolve(
        self,
        identifier: str,  # noqa: ARG002
        provider_hint: Provider | None = None,  # noqa: ARG002
    ) -> ResolvedRepository:
        return ResolvedRepository(
            repo_ref=self._repo_ref,
            connection=self._connection,
            adapter=None,
        )


@pytest.mark.asyncio
async def test_webhook_verification(github_adapter: GitHubAdapter, webhook_secret: str):
    """Test webhook signature verification using conformance suite."""
    payload = {"test": "data"}
    body = json.dumps(payload).encode()

    valid_headers = {
        "X-Hub-Signature-256": sign_webhook(payload, webhook_secret),
    }

    invalid_headers = {
        "X-Hub-Signature-256": "sha256=invalid",
    }

    # Pass webhook_secret to adapter for verification
    adapter_with_secret = GitHubAdapter(
        github_adapter._connection,
        credential="test-token",
        webhook_secret=webhook_secret,
    )

    await assert_webhook_verification(adapter_with_secret, valid_headers, body, invalid_headers)


@pytest.mark.asyncio
async def test_webhook_verification_fails_closed_when_secret_unset(
    github_adapter: GitHubAdapter,
):
    """An adapter with no configured webhook_secret must reject every signature,
    not silently verify against an empty-string HMAC key (which anyone could forge).
    """
    payload = {"test": "data"}
    body = json.dumps(payload).encode()

    # Forged using the empty-string key an unset secret would otherwise fall back to.
    forged_headers = {
        "X-Hub-Signature-256": sign_webhook(payload, ""),
    }

    assert github_adapter._webhook_secret is None
    assert await github_adapter.verify_webhook(forged_headers, body) is False


@pytest.mark.asyncio
async def test_webhook_parsing_pr_opened(
    github_adapter: GitHubAdapter,
    github_repo_ref: RepositoryRef,
    github_connection: Connection,
    valid_pr_opened_payload: dict,
):
    """Test parsing pull_request opened webhook."""
    body = json.dumps(valid_pr_opened_payload).encode()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "test-delivery-123",
    }

    resolver = MockResolver(github_repo_ref, github_connection)

    await assert_webhook_parsing(
        github_adapter,
        headers,
        body,
        resolver,
        expected_kind=EventKind.CR_OPENED,
        expected_repo_namespace="test/repo",
    )


class TestGetClientCredentialThreading:
    """_get_client() must thread self._credential into the constructed client."""

    def test_uses_injected_client_over_credential(
        self,
        github_connection: Connection,
        mock_github_http_client: GitHubClient,
    ):
        adapter = GitHubAdapter(
            github_connection,
            credential="ignored-because-client-injected",
            client=mock_github_http_client,
        )

        assert adapter._get_client() is mock_github_http_client

    def test_builds_client_using_configured_credential(self, github_connection: Connection):
        adapter = GitHubAdapter(github_connection, credential="per-connection-token")

        client = adapter._get_client()

        assert isinstance(client, GitHubClient)
        assert client.settings.github_token.get_secret_value() == "per-connection-token"

    def test_falls_back_to_process_settings_when_no_credential(self, github_connection: Connection):
        from forge.config import get_settings

        adapter = GitHubAdapter(github_connection)

        client = adapter._get_client()

        assert isinstance(client, GitHubClient)
        assert (
            client.settings.github_token.get_secret_value()
            == get_settings().github_token.get_secret_value()
        )

    def test_lazily_constructed_client_is_cached(self, github_connection: Connection):
        adapter = GitHubAdapter(github_connection, credential="token-a")

        first = adapter._get_client()
        second = adapter._get_client()

        assert first is second


class TestResolveDefaultBranch:
    @pytest.mark.asyncio
    async def test_returns_default_branch_from_api(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_client = mock_github_http_client._client
        response = MagicMock()
        response.json.return_value = {"default_branch": "develop", "full_name": "test/repo"}
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        branch = await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

        mock_client.get.assert_called_once_with("/repos/test/repo")
        assert branch == "develop"

    @pytest.mark.asyncio
    async def test_falls_back_to_main_when_field_missing(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_client = mock_github_http_client._client
        response = MagicMock()
        response.json.return_value = {"full_name": "test/repo"}
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        branch = await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

        assert branch == "main"


class TestTranslateProviderErrors:
    """Direct coverage for the `_translate_provider_errors` boundary decorator
    applied to every adapter method that calls the GitHub API."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "expected_exception"),
        [
            (401, AuthenticationError),
            (403, AuthenticationError),
            (429, RateLimitedError),
            (500, TransientProviderError),
            (503, TransientProviderError),
        ],
    )
    async def test_status_code_maps_to_neutral_exception(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
        status_code: int,
        expected_exception: type[Exception],
    ):
        response = httpx.Response(
            status_code,
            headers={"Retry-After": "30"} if status_code == 429 else {},
            request=httpx.Request("GET", "https://api.github.com/repos/test/repo"),
        )
        mock_github_http_client.get_repository = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(expected_exception):
            await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

    @pytest.mark.asyncio
    async def test_rate_limit_parses_retry_after(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        response = httpx.Response(
            429,
            headers={"Retry-After": "30"},
            request=httpx.Request("GET", "https://api.github.com/repos/test/repo"),
        )
        mock_github_http_client.get_repository = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(RateLimitedError) as exc_info:
            await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

        assert exc_info.value.retry_after == 30.0

    @pytest.mark.asyncio
    async def test_rate_limit_with_non_numeric_retry_after_falls_back_to_none(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """An HTTP-date Retry-After value (valid per RFC 7231) must not crash
        the error-translation path with an unhandled ValueError."""
        response = httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            request=httpx.Request("GET", "https://api.github.com/repos/test/repo"),
        )
        mock_github_http_client.get_repository = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(RateLimitedError) as exc_info:
            await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

        assert exc_info.value.retry_after is None

    @pytest.mark.asyncio
    async def test_network_failure_maps_to_transient_provider_error(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_github_http_client.get_repository = AsyncMock(
            side_effect=httpx.ConnectTimeout("connection timed out")
        )

        with pytest.raises(TransientProviderError):
            await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)

    @pytest.mark.asyncio
    async def test_not_found_status_propagates_unchanged(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """404 has no generic neutral mapping and must be left for callers to
        handle themselves rather than being swallowed or re-mapped."""
        response = httpx.Response(
            404, request=httpx.Request("GET", "https://api.github.com/repos/test/repo")
        )
        mock_github_http_client.get_repository = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )

        with pytest.raises(httpx.HTTPStatusError):
            await github_adapter_with_mock_client.resolve_default_branch(github_repo_ref)


class TestGetAuthenticatedIdentity:
    @pytest.mark.asyncio
    async def test_returns_human_actor(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_client = mock_github_http_client._client
        response = MagicMock()
        response.json.return_value = {"login": "octocat", "type": "User"}
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        actor = await github_adapter_with_mock_client.get_authenticated_identity(github_repo_ref)

        mock_client.get.assert_called_once_with("/user")
        assert actor.login == "octocat"
        assert actor.is_bot is False

    @pytest.mark.asyncio
    async def test_detects_bot_via_type_field(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_client = mock_github_http_client._client
        response = MagicMock()
        response.json.return_value = {"login": "forge-bot", "type": "Bot"}
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        actor = await github_adapter_with_mock_client.get_authenticated_identity(github_repo_ref)

        assert actor.is_bot is True

    @pytest.mark.asyncio
    async def test_detects_bot_via_login_suffix(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        mock_client = mock_github_http_client._client
        response = MagicMock()
        response.json.return_value = {"login": "dependabot[bot]", "type": "User"}
        response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=response)

        actor = await github_adapter_with_mock_client.get_authenticated_identity(github_repo_ref)

        assert actor.is_bot is True


class TestEnsureWriteTarget:
    @pytest.mark.asyncio
    async def test_fork_mode_creates_and_syncs_fork(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """Fork mode creates/reuses a fork, syncs it, and derives the target
        coordinates from the fork API response."""
        mock_github_http_client.get_or_create_fork = AsyncMock(
            return_value={
                "name": "repo",
                "owner": {"login": "forge-bot"},
                "clone_url": "https://github.com/forge-bot/repo.git",
                "default_branch": "main",
            }
        )
        mock_github_http_client.sync_fork_with_upstream = AsyncMock(return_value=True)

        target = await github_adapter_with_mock_client.ensure_write_target(github_repo_ref)

        mock_github_http_client.get_or_create_fork.assert_awaited_once_with("test", "repo")
        mock_github_http_client.sync_fork_with_upstream.assert_awaited_once_with(
            "forge-bot", "repo", branch="main"
        )
        assert target.clone_url == "https://github.com/forge-bot/repo.git"
        assert target.push_remote_name == "origin"
        assert target.head_ref == "forge/test/repo"
        assert target.base_branch == "main"

    @pytest.mark.asyncio
    async def test_fork_mode_raises_conflict_when_fork_diverged(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A diverged fork (sync returns False) surfaces as ConflictError."""
        mock_github_http_client.get_or_create_fork = AsyncMock(
            return_value={
                "name": "repo",
                "owner": {"login": "forge-bot"},
                "clone_url": "https://github.com/forge-bot/repo.git",
            }
        )
        mock_github_http_client.sync_fork_with_upstream = AsyncMock(return_value=False)

        with pytest.raises(ConflictError, match="diverged"):
            await github_adapter_with_mock_client.ensure_write_target(github_repo_ref)

    @pytest.mark.asyncio
    async def test_direct_mode_makes_no_api_calls(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        mock_github_http_client: GitHubClient,
    ):
        """Direct mode targets upstream directly and touches no HTTP/client methods."""
        direct_repo_ref = RepositoryRef(
            id="test/repo",
            provider=Provider.GITHUB,
            connection="test-github",
            namespace="test/repo",
            default_branch="develop",
            change_request_mode="direct",
        )

        target = await github_adapter_with_mock_client.ensure_write_target(direct_repo_ref)

        assert target.clone_url == "https://github.com/test/repo.git"
        assert target.push_remote_name == "origin"
        assert target.head_ref == "forge/test/repo"
        assert target.base_branch == "develop"
        # No fork/sync/HTTP work should have happened in direct mode.
        mock_github_http_client._client.get.assert_not_called()
        mock_github_http_client._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_mode_derives_clone_url_from_enterprise_connection(self):
        """A GitHub Enterprise Server connection's web host (no 'api.' subdomain,
        no '/api/v3' suffix) must be used for the clone URL, not github.com."""
        enterprise_connection = Connection(
            name="ghe",
            provider=Provider.GITHUB,
            base_url="https://ghe.example.com/api/v3",
            credential_env="GHE_TOKEN",
            webhook_secret_env="GHE_WEBHOOK_SECRET",
        )
        adapter = GitHubAdapter(enterprise_connection, credential="test-token-123")
        direct_repo_ref = RepositoryRef(
            id="test/repo",
            provider=Provider.GITHUB,
            connection="ghe",
            namespace="test/repo",
            default_branch="main",
            change_request_mode="direct",
        )

        target = await adapter.ensure_write_target(direct_repo_ref)

        assert target.clone_url == "https://ghe.example.com/test/repo.git"


class TestLazyClientConnectionPlumbing:
    def test_lazy_client_uses_connection_base_url_and_ca_path(self):
        """GitHubAdapter's lazily-constructed client must target the configured
        connection's API host and CA bundle, not the public GitHub defaults."""
        enterprise_connection = Connection(
            name="ghe",
            provider=Provider.GITHUB,
            base_url="https://ghe.example.com/api/v3",
            credential_env="GHE_TOKEN",
            webhook_secret_env="GHE_WEBHOOK_SECRET",
            ca_path="/etc/ssl/certs/ghe-ca.pem",
        )
        adapter = GitHubAdapter(enterprise_connection, credential="test-token-123")

        client = adapter._get_client()

        assert client.base_url == "https://ghe.example.com/api/v3"
        assert client._ca_path == "/etc/ssl/certs/ghe-ca.pem"

    @pytest.mark.asyncio
    async def test_close_closes_the_lazily_constructed_client(self, github_connection: Connection):
        adapter = GitHubAdapter(github_connection, credential="test-token-123")
        client = adapter._get_client()
        client.close = AsyncMock()

        await adapter.close()

        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_a_noop_when_no_client_was_ever_constructed(
        self, github_connection: Connection
    ):
        adapter = GitHubAdapter(github_connection, credential="test-token-123")

        await adapter.close()  # must not raise, must not construct a client

        assert adapter._client is None


class TestGetGitCredentials:
    @pytest.mark.asyncio
    async def test_public_github_derives_bare_host_and_credential(
        self, github_adapter: GitHubAdapter, github_repo_ref: RepositoryRef
    ):
        """Public GitHub's web host (github.com) must be used for git
        operations, not the API host (api.github.com)."""
        credentials = await github_adapter.get_git_credentials(github_repo_ref)

        assert credentials.host == "github.com"
        assert credentials.token == "test-token-123"
        assert credentials.ca_path is None

    @pytest.mark.asyncio
    async def test_enterprise_connection_derives_web_host_and_ca_path(
        self, github_repo_ref: RepositoryRef
    ):
        """An Enterprise Server connection's git host has no /api/v3 suffix
        (unlike its API base_url), and its CA bundle must be carried through
        for git's own TLS verification."""
        enterprise_connection = Connection(
            name="ghe",
            provider=Provider.GITHUB,
            base_url="https://ghe.example.com/api/v3",
            credential_env="GHE_TOKEN",
            webhook_secret_env="GHE_WEBHOOK_SECRET",
            ca_path="/etc/ssl/certs/ghe-ca.pem",
        )
        adapter = GitHubAdapter(enterprise_connection, credential="ghe-token-456")

        credentials = await adapter.get_git_credentials(github_repo_ref)

        assert credentials.host == "ghe.example.com"
        assert credentials.token == "ghe-token-456"
        assert credentials.ca_path == "/etc/ssl/certs/ghe-ca.pem"


@pytest.fixture
def write_target() -> WriteTarget:
    return WriteTarget(
        clone_url="https://github.com/forge-bot/repo.git",
        push_remote_name="origin",
        head_ref="forge/test/repo",
        base_branch="main",
    )


def _pr_dict(**overrides) -> dict:
    """A representative GitHub PR API response, with optional field overrides."""
    pr = {
        "number": 42,
        "html_url": "https://github.com/test/repo/pull/42",
        "title": "Test PR",
        "body": "Test body",
        "state": "open",
        "merged": False,
        "draft": False,
        "head": {"ref": "forge/test/repo"},
        "base": {"ref": "main"},
    }
    pr.update(overrides)
    return pr


class TestCreateChangeRequest:
    @pytest.mark.asyncio
    async def test_creates_pr_and_maps_result(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
        write_target: WriteTarget,
    ):
        mock_github_http_client.create_pull_request = AsyncMock(
            return_value=PullRequestCreationResult(pr=_pr_dict(), created=True)
        )

        cr = await github_adapter_with_mock_client.create_change_request(
            github_repo_ref,
            write_target,
            title="Test PR",
            body="Test body",
            draft=False,
        )

        mock_github_http_client.create_pull_request.assert_awaited_once_with(
            owner="test",
            repo="repo",
            title="Test PR",
            body="Test body",
            head="forge/test/repo",
            base="main",
            draft=False,
        )
        assert cr.identity == ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        assert cr.url == "https://github.com/test/repo/pull/42"
        assert cr.title == "Test PR"
        assert cr.body == "Test body"
        assert cr.state == ChangeRequestState.OPEN
        assert cr.source_branch == "forge/test/repo"
        assert cr.target_branch == "main"
        assert cr.draft is False

    @pytest.mark.asyncio
    async def test_returns_existing_pr_when_already_present(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
        write_target: WriteTarget,
    ):
        """The 422-already-exists path returns created=False with the existing PR,
        which is mapped and returned like any other PR."""
        mock_github_http_client.create_pull_request = AsyncMock(
            return_value=PullRequestCreationResult(
                pr=_pr_dict(number=7, html_url="https://github.com/test/repo/pull/7"),
                created=False,
            )
        )

        cr = await github_adapter_with_mock_client.create_change_request(
            github_repo_ref,
            write_target,
            title="Test PR",
            body="Test body",
        )

        assert cr.identity.native_id == 7
        assert cr.url == "https://github.com/test/repo/pull/7"
        assert cr.state == ChangeRequestState.OPEN

    @pytest.mark.asyncio
    async def test_draft_flag_is_forwarded(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
        write_target: WriteTarget,
    ):
        mock_github_http_client.create_pull_request = AsyncMock(
            return_value=PullRequestCreationResult(pr=_pr_dict(draft=True), created=True)
        )

        cr = await github_adapter_with_mock_client.create_change_request(
            github_repo_ref,
            write_target,
            title="Test PR",
            body="Test body",
            draft=True,
        )

        assert mock_github_http_client.create_pull_request.await_args.kwargs["draft"] is True
        assert cr.draft is True


def test_map_change_request_rejects_both_repo_ref_and_identity(
    github_adapter_with_mock_client: GitHubAdapter,
    github_repo_ref: RepositoryRef,
):
    """_map_change_request's contract is "exactly one of repo_ref or identity" --
    passing both must raise rather than silently letting identity win."""
    identity = ChangeRequestIdentity(
        connection="test-github", repository_id="test/repo", native_id=1
    )

    with pytest.raises(ValueError, match="not both"):
        github_adapter_with_mock_client._map_change_request(
            _pr_dict(), repo_ref=github_repo_ref, identity=identity
        )


class TestGetChangeRequest:
    @pytest.mark.asyncio
    async def test_fetches_and_preserves_identity(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.get_pull_request = AsyncMock(
            return_value=_pr_dict(state="closed", merged=True)
        )

        cr = await github_adapter_with_mock_client.get_change_request(github_repo_ref, identity)

        mock_github_http_client.get_pull_request.assert_awaited_once_with("test", "repo", 42)
        # The exact identity object passed in is preserved, not reconstructed.
        assert cr.identity is identity
        assert cr.state == ChangeRequestState.MERGED

    @pytest.mark.asyncio
    async def test_coerces_string_native_id_to_int(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id="99"
        )
        mock_github_http_client.get_pull_request = AsyncMock(return_value=_pr_dict(number=99))

        await github_adapter_with_mock_client.get_change_request(github_repo_ref, identity)

        mock_github_http_client.get_pull_request.assert_awaited_once_with("test", "repo", 99)

    @pytest.mark.asyncio
    async def test_raises_on_missing_native_id_instead_of_defaulting_to_zero(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=None
        )
        mock_github_http_client.get_pull_request = AsyncMock()

        with pytest.raises(ValueError, match="native_id"):
            await github_adapter_with_mock_client.get_change_request(github_repo_ref, identity)

        mock_github_http_client.get_pull_request.assert_not_awaited()


class TestUpdateChangeRequest:
    @pytest.mark.asyncio
    async def test_updates_title_and_body(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.update_pull_request = AsyncMock(
            return_value=_pr_dict(title="New title", body="New body")
        )

        cr = await github_adapter_with_mock_client.update_change_request(
            github_repo_ref,
            identity,
            title="New title",
            body="New body",
        )

        mock_github_http_client.update_pull_request.assert_awaited_once_with(
            owner="test",
            repo="repo",
            pr_number=42,
            title="New title",
            body="New body",
            state=None,
        )
        assert cr.identity is identity
        assert cr.title == "New title"
        assert cr.body == "New body"

    @pytest.mark.asyncio
    async def test_maps_closed_state_to_github_string(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.update_pull_request = AsyncMock(
            return_value=_pr_dict(state="closed")
        )

        cr = await github_adapter_with_mock_client.update_change_request(
            github_repo_ref,
            identity,
            state=ChangeRequestState.CLOSED,
        )

        assert mock_github_http_client.update_pull_request.await_args.kwargs["state"] == "closed"
        assert cr.state == ChangeRequestState.CLOSED

    @pytest.mark.asyncio
    async def test_maps_open_state_to_github_string(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.update_pull_request = AsyncMock(return_value=_pr_dict(state="open"))

        await github_adapter_with_mock_client.update_change_request(
            github_repo_ref,
            identity,
            state=ChangeRequestState.OPEN,
        )

        assert mock_github_http_client.update_pull_request.await_args.kwargs["state"] == "open"

    @pytest.mark.asyncio
    async def test_rejects_merged_state(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """MERGED cannot be set via the update endpoint; the adapter raises rather
        than silently ignoring it or closing the PR instead."""
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.update_pull_request = AsyncMock()

        with pytest.raises(ValueError, match="MERGED"):
            await github_adapter_with_mock_client.update_change_request(
                github_repo_ref,
                identity,
                state=ChangeRequestState.MERGED,
            )

        # No API call should have been made when the state is rejected.
        mock_github_http_client.update_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_on_missing_native_id_instead_of_defaulting_to_zero(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """_require_native_id's guard applies here too, not just get_change_request."""
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=None
        )
        mock_github_http_client.update_pull_request = AsyncMock()

        with pytest.raises(ValueError, match="native_id"):
            await github_adapter_with_mock_client.update_change_request(
                github_repo_ref, identity, title="New title"
            )

        mock_github_http_client.update_pull_request.assert_not_awaited()


class TestCreateComment:
    @pytest.mark.asyncio
    async def test_creates_issue_comment_and_maps_result(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.create_issue_comment = AsyncMock(
            return_value={
                "id": 555,
                "body": "Looks good",
                "user": {"login": "forge-bot"},
            }
        )

        comment = await github_adapter_with_mock_client.create_comment(
            github_repo_ref, identity, "Looks good"
        )

        mock_github_http_client.create_issue_comment.assert_awaited_once_with(
            owner="test",
            repo="repo",
            issue_number=42,
            body="Looks good",
        )
        assert comment.id == "555"
        assert comment.body == "Looks good"
        assert comment.author == "forge-bot"
        assert comment.in_reply_to is None

    @pytest.mark.asyncio
    async def test_coerces_string_native_id_to_int(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id="99"
        )
        mock_github_http_client.create_issue_comment = AsyncMock(
            return_value={"id": 1, "body": "x", "user": {"login": "forge-bot"}}
        )

        await github_adapter_with_mock_client.create_comment(github_repo_ref, identity, "x")

        assert mock_github_http_client.create_issue_comment.await_args.kwargs["issue_number"] == 99

    @pytest.mark.asyncio
    async def test_handles_null_user_from_deleted_account(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """GitHub returns "user": null for comments from a deleted account —
        this must not crash with AttributeError on None.get(...)."""
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.create_issue_comment = AsyncMock(
            return_value={"id": 1, "body": "x", "user": None}
        )

        comment = await github_adapter_with_mock_client.create_comment(
            github_repo_ref, identity, "x"
        )

        assert comment.author == ""


class TestReplyToComment:
    @pytest.mark.asyncio
    async def test_uses_reply_endpoint_and_sets_in_reply_to(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.reply_to_review_comment = AsyncMock(
            return_value={
                "id": 777,
                "body": "Thanks",
                "user": {"login": "forge-bot"},
                "path": "src/foo.py",
                "line": 10,
            }
        )
        # Ensure the generic issue-comment endpoint is NOT used for replies.
        mock_github_http_client.create_issue_comment = AsyncMock()

        comment = await github_adapter_with_mock_client.reply_to_comment(
            github_repo_ref, identity, comment_id="123", body="Thanks"
        )

        mock_github_http_client.reply_to_review_comment.assert_awaited_once_with(
            owner="test",
            repo="repo",
            pr_number=42,
            comment_id=123,
            body="Thanks",
        )
        mock_github_http_client.create_issue_comment.assert_not_awaited()
        assert comment.id == "777"
        assert comment.body == "Thanks"
        assert comment.author == "forge-bot"
        assert comment.path == "src/foo.py"
        assert comment.line == 10
        assert comment.in_reply_to == "123"


class TestGetReviewThreads:
    @pytest.mark.asyncio
    async def test_maps_approved_and_changes_requested(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.get_reviews = AsyncMock(
            return_value=[
                {"id": 1, "state": "APPROVED", "body": "LGTM", "user": {"login": "alice"}},
                {"id": 2, "state": "CHANGES_REQUESTED", "body": "Fix", "user": {"login": "bob"}},
            ]
        )

        reviews = await github_adapter_with_mock_client.get_review_threads(
            github_repo_ref, identity
        )

        mock_github_http_client.get_reviews.assert_awaited_once_with("test", "repo", 42)
        assert [r.id for r in reviews] == ["1", "2"]
        assert reviews[0].state == ReviewState.APPROVED
        assert reviews[0].body == "LGTM"
        assert reviews[0].author == "alice"
        assert reviews[0].comments == []
        assert reviews[1].state == ReviewState.CHANGES_REQUESTED
        assert reviews[1].author == "bob"
        assert reviews[1].comments == []

    @pytest.mark.asyncio
    async def test_handles_null_user_from_deleted_account(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """GitHub returns "user": null for a review from a deleted account —
        this must not crash with AttributeError on None.get(...)."""
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.get_reviews = AsyncMock(
            return_value=[{"id": 3, "state": "COMMENTED", "body": None, "user": None}]
        )

        reviews = await github_adapter_with_mock_client.get_review_threads(
            github_repo_ref, identity
        )

        assert reviews[0].author == ""
        assert reviews[0].body == ""
        assert reviews[0].state == ReviewState.COMMENTED

    @pytest.mark.asyncio
    async def test_dismissed_maps_to_dismissed(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """DISMISSED has its own dedicated ReviewState member, kept distinct
        from COMMENTED so a withdrawn review isn't mistaken for an active one
        requesting changes (an admin dismissing a stale review must not
        trigger a revision request downstream)."""
        identity = ChangeRequestIdentity(
            connection="test-github", repository_id="test/repo", native_id=42
        )
        mock_github_http_client.get_reviews = AsyncMock(
            return_value=[
                {"id": 4, "state": "DISMISSED", "body": "old", "user": {"login": "carol"}},
                {"id": 5, "state": "PENDING", "body": "", "user": {"login": "dave"}},
            ]
        )

        reviews = await github_adapter_with_mock_client.get_review_threads(
            github_repo_ref, identity
        )

        assert reviews[0].state == ReviewState.DISMISSED
        assert reviews[1].state == ReviewState.PENDING


class TestGetChecks:
    @pytest.mark.asyncio
    async def test_maps_actions_backed_check_run_with_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """An Actions-backed check run stores the workflow *run* id (parsed from
        details_url) in logs_url -- deliberately distinct from the check-run id,
        since a check-run id is not an Actions job id."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {
                    "id": 12345,  # check-run id: NOT the run id, NOT the job id
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/test/repo/runs/12345",
                    "details_url": "https://github.com/test/repo/actions/runs/987654",
                    "app": {"slug": "github-actions"},
                }
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        mock_github_http_client.get_check_runs.assert_awaited_once_with(
            owner="test", repo="repo", ref="abc123"
        )
        assert len(checks) == 1
        check = checks[0]
        assert check.name == "build"
        assert check.status == CheckStatus.COMPLETED
        assert check.conclusion == CheckConclusion.SUCCESS
        assert check.url == "https://github.com/test/repo/runs/12345"
        # The run id from details_url, not the check-run id.
        assert check.logs_url == "987654"

    @pytest.mark.asyncio
    async def test_non_actions_app_with_numeric_id_has_no_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A check run from a non-Actions GitHub App (e.g. CodeQL) is not backed
        by an Actions job -- even though it carries a numeric id and an
        actions-style details_url, logs_url must stay None rather than pointing
        get_check_logs at a run that has no matching Actions job."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {
                    "id": 99999,
                    "name": "CodeQL",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/test/repo/runs/99999",
                    "details_url": "https://github.com/test/repo/actions/runs/99999",
                    "app": {"slug": "github-code-scanning"},
                }
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        assert checks[0].logs_url is None

    @pytest.mark.asyncio
    async def test_actions_check_without_run_id_in_details_url_has_no_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """An Actions check whose details_url isn't the expected
        /actions/runs/{id} shape yields no resolvable run id, so logs_url stays
        None rather than storing a bogus resolution key."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {
                    "id": 12345,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://example.com/some/other/path",
                    "app": {"slug": "github-actions"},
                }
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        assert checks[0].logs_url is None

    @pytest.mark.asyncio
    async def test_timed_out_and_action_required_map_to_failure(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """timed_out and action_required both indicate the check did not pass,
        so they must not collapse into the same NONE bucket as "no conclusion
        yet"."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {"id": 1, "name": "slow-job", "status": "completed", "conclusion": "timed_out"},
                {"id": 2, "name": "gate", "status": "completed", "conclusion": "action_required"},
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        assert checks[0].conclusion == CheckConclusion.FAILURE
        assert checks[1].conclusion == CheckConclusion.FAILURE

    @pytest.mark.asyncio
    async def test_maps_commit_status_backed_entry_with_no_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A commit-status-backed check (per _normalize_commit_status) has no id,
        so logs_url must be None — there is no logs endpoint for it."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {
                    "name": "prow/verify",
                    "status": "completed",
                    "conclusion": "failure",
                    "output": {"summary": "it broke"},
                    "html_url": "https://prow.example.com/log",
                }
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        assert len(checks) == 1
        check = checks[0]
        assert check.name == "prow/verify"
        assert check.status == CheckStatus.COMPLETED
        assert check.conclusion == CheckConclusion.FAILURE
        assert check.url == "https://prow.example.com/log"
        assert check.logs_url is None

    @pytest.mark.asyncio
    async def test_unknown_conclusion_maps_to_none(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A missing/unrecognized conclusion becomes CheckConclusion.NONE, and a
        missing status with no conclusion is inferred as IN_PROGRESS."""
        mock_github_http_client.get_check_runs = AsyncMock(
            return_value=[
                {"id": 1, "name": "pending-check", "status": "", "conclusion": None},
            ]
        )

        checks = await github_adapter_with_mock_client.get_checks(github_repo_ref, "abc123")

        assert checks[0].conclusion == CheckConclusion.NONE
        assert checks[0].status == CheckStatus.IN_PROGRESS
        assert checks[0].url == ""


class TestGetCheckLogs:
    @pytest.mark.asyncio
    async def test_fetches_logs_for_actions_backed_check(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """logs_url holds the workflow *run* id; the adapter lists the run's
        jobs, matches by name, and fetches the *job* id's logs. Run id, job id,
        and the original check-run id are all deliberately distinct so the test
        can't pass by treating any of them as interchangeable."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock(
            return_value=[
                {"id": 555, "name": "lint"},
                {"id": 777, "name": "build"},
            ]
        )
        mock_github_http_client.get_job_logs = AsyncMock(return_value="line 1\nline 2\n")
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            url="https://github.com/test/repo/runs/12345",
            logs_url="987654",  # workflow run id, not the check-run or job id
        )

        logs = await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

        mock_github_http_client.list_workflow_run_jobs.assert_awaited_once_with(
            "test", "repo", 987654
        )
        # The resolved job id (777), NOT the run id (987654) or check-run id.
        mock_github_http_client.get_job_logs.assert_awaited_once_with("test", "repo", job_id=777)
        assert logs == "line 1\nline 2\n"

    @pytest.mark.asyncio
    async def test_raises_not_found_when_no_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A commit-status check (logs_url None) has no logs endpoint, so
        requesting its logs raises NotFoundError without any HTTP call."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock()
        mock_github_http_client.get_job_logs = AsyncMock()
        check = CheckRun(
            name="prow/verify",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.FAILURE,
            url="https://prow.example.com/log",
            logs_url=None,
        )

        with pytest.raises(NotFoundError, match="No logs available"):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

        mock_github_http_client.list_workflow_run_jobs.assert_not_awaited()
        mock_github_http_client.get_job_logs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_source_control_error_for_non_numeric_logs_url(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """logs_url survives a round-trip through the Redis queue as a raw string;
        a malformed/legacy value must raise the documented SourceControlError
        rather than leaking an uncaught ValueError, and must not hit the API."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock()
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="not-a-number",
        )

        with pytest.raises(SourceControlError, match="non-numeric logs_url"):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

        mock_github_http_client.list_workflow_run_jobs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_not_found_when_no_job_matches_check_name(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """If the run has no job whose name matches the check, there is no job id
        to fetch logs for -- raise NotFoundError rather than guessing."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock(
            return_value=[
                {"id": 555, "name": "lint"},
                {"id": 666, "name": "test"},
            ]
        )
        mock_github_http_client.get_job_logs = AsyncMock()
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        with pytest.raises(NotFoundError, match="No Actions job named 'build'"):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

        mock_github_http_client.get_job_logs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_when_multiple_jobs_match_check_name(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """If more than one job in the run shares the check's name, the correct
        logs can't be chosen unambiguously -- raise rather than fetch the wrong
        job's logs."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock(
            return_value=[
                {"id": 777, "name": "build"},
                {"id": 888, "name": "build"},
            ]
        )
        mock_github_http_client.get_job_logs = AsyncMock()
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        with pytest.raises(SourceControlError, match="2 jobs named 'build'"):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

        mock_github_http_client.get_job_logs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_translates_404_into_not_found(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A 404 httpx.HTTPStatusError from get_job_logs is translated into the
        provider-neutral NotFoundError."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock(
            return_value=[{"id": 777, "name": "build"}]
        )
        response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/logs"))
        mock_github_http_client.get_job_logs = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not found", request=response.request, response=response
            )
        )
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            url="https://github.com/test/repo/runs/12345",
            logs_url="987654",
        )

        with pytest.raises(NotFoundError, match="were not"):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)

    @pytest.mark.asyncio
    async def test_non_404_http_error_propagates(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """Non-404 HTTP errors are not swallowed as NotFoundError; they are
        translated into the neutral error hierarchy at the adapter boundary
        instead of leaking a raw httpx exception."""
        mock_github_http_client.list_workflow_run_jobs = AsyncMock(
            return_value=[{"id": 777, "name": "build"}]
        )
        response = httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/logs"))
        mock_github_http_client.get_job_logs = AsyncMock(
            side_effect=httpx.HTTPStatusError("boom", request=response.request, response=response)
        )
        check = CheckRun(
            name="build",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            logs_url="987654",
        )

        with pytest.raises(TransientProviderError):
            await github_adapter_with_mock_client.get_check_logs(github_repo_ref, check)


@pytest.mark.asyncio
async def test_repository_operations(
    github_adapter_with_mock_client: GitHubAdapter,
    github_repo_ref: RepositoryRef,
    mock_github_http_client: GitHubClient,
):
    """Conformance suite coverage for repository metadata operations.

    Exercises resolve_default_branch and get_authenticated_identity back-to-back
    on the same adapter/client instance, which also guards against per-call
    client teardown silently swapping the mocked httpx.AsyncClient for a real one.
    """
    mock_client = mock_github_http_client._client

    repo_response = MagicMock()
    repo_response.json.return_value = {"default_branch": "main", "full_name": "test/repo"}
    repo_response.raise_for_status = MagicMock()

    user_response = MagicMock()
    user_response.json.return_value = {"login": "octocat", "type": "User"}
    user_response.raise_for_status = MagicMock()

    mock_client.get = AsyncMock(side_effect=[repo_response, user_response])

    await assert_repository_operations(github_adapter_with_mock_client, github_repo_ref)


class TestGetFile:
    @pytest.mark.asyncio
    async def test_returns_decoded_content(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A found file's base64 content (with the embedded newlines GitHub's API
        wraps it in) round-trips back to the original UTF-8 text."""
        original = "line one\nline two\nline three\n" * 5
        # GitHub wraps base64 content at column 76 with embedded newlines.
        encoded = base64.encodebytes(original.encode()).decode()
        assert "\n" in encoded.strip()  # sanity: multi-line payload
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={
                "content": encoded,
                "encoding": "base64",
                "sha": "abc123",
                "path": "README.md",
            }
        )

        content = await github_adapter_with_mock_client.get_file(
            github_repo_ref, "README.md", "main"
        )

        mock_github_http_client.get_file_contents.assert_awaited_once_with(
            owner="test", repo="repo", path="README.md", ref="main"
        )
        assert content == original

    @pytest.mark.asyncio
    async def test_raises_not_found_when_missing(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A missing file (client returns None for 404) surfaces as NotFoundError,
        not a silent empty string, because the protocol return type is str."""
        mock_github_http_client.get_file_contents = AsyncMock(return_value=None)

        with pytest.raises(NotFoundError, match="not found"):
            await github_adapter_with_mock_client.get_file(github_repo_ref, "missing.txt", "main")

    @pytest.mark.asyncio
    async def test_raises_on_file_too_large_for_inline_content(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """GitHub only inlines base64 content for files <=1MB; larger files come
        back with encoding "none" and empty content on an otherwise-successful
        response. That must raise, not silently look like an empty file."""
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={"content": "", "encoding": "none", "size": 5_000_000, "sha": "big-sha"}
        )

        with pytest.raises(SourceControlError, match="1MB"):
            await github_adapter_with_mock_client.get_file(github_repo_ref, "big.bin", "main")


class TestPutFile:
    @pytest.mark.asyncio
    async def test_creates_new_file_without_sha(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """When the file doesn't exist yet (lookup returns None), no sha is passed
        so the Contents API treats it as a create."""
        mock_github_http_client.get_file_contents = AsyncMock(return_value=None)
        mock_github_http_client.create_or_update_file = AsyncMock(return_value={})

        await github_adapter_with_mock_client.put_file(
            github_repo_ref,
            path="docs/new.md",
            content="hello",
            message="add new.md",
            branch="feature",
        )

        mock_github_http_client.get_file_contents.assert_awaited_once_with(
            owner="test", repo="repo", path="docs/new.md", ref="feature"
        )
        mock_github_http_client.create_or_update_file.assert_awaited_once_with(
            owner="test",
            repo="repo",
            path="docs/new.md",
            content="hello",
            message="add new.md",
            branch="feature",
            sha=None,
        )

    @pytest.mark.asyncio
    async def test_updates_existing_file_passes_sha_through(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """When the file already exists, its sha from the prior lookup is threaded
        into the update call (a create-without-sha would 422)."""
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={"content": "b2xk", "sha": "existing-sha-999"}
        )
        mock_github_http_client.create_or_update_file = AsyncMock(return_value={})

        await github_adapter_with_mock_client.put_file(
            github_repo_ref,
            path="docs/existing.md",
            content="updated body",
            message="update existing.md",
            branch="main",
        )

        mock_github_http_client.get_file_contents.assert_awaited_once_with(
            owner="test", repo="repo", path="docs/existing.md", ref="main"
        )
        mock_github_http_client.create_or_update_file.assert_awaited_once_with(
            owner="test",
            repo="repo",
            path="docs/existing.md",
            content="updated body",
            message="update existing.md",
            branch="main",
            sha="existing-sha-999",
        )

    @pytest.mark.asyncio
    async def test_translates_stale_sha_conflict(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A concurrent write between the sha lookup and the update surfaces as
        a 409/422 from GitHub; that must translate into ConflictError rather
        than propagate as a raw httpx error."""
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={"content": "b2xk", "sha": "stale-sha"}
        )
        response = httpx.Response(
            409, request=httpx.Request("PUT", "https://api.github.com/contents")
        )
        mock_github_http_client.create_or_update_file = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "conflict", request=response.request, response=response
            )
        )

        with pytest.raises(ConflictError, match="concurrently modified"):
            await github_adapter_with_mock_client.put_file(
                github_repo_ref,
                path="docs/existing.md",
                content="updated body",
                message="update existing.md",
                branch="main",
            )

    @pytest.mark.asyncio
    async def test_translates_422_with_sha_message_to_conflict(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A 422 whose error message mentions the sha mismatch is the same
        stale-sha conflict as a 409 -- must still translate to ConflictError."""
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={"content": "b2xk", "sha": "stale-sha"}
        )
        response = httpx.Response(
            422,
            json={"message": "docs/existing.md does not match stale-sha"},
            request=httpx.Request("PUT", "https://api.github.com/contents"),
        )
        mock_github_http_client.create_or_update_file = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "unprocessable", request=response.request, response=response
            )
        )

        with pytest.raises(ConflictError, match="concurrently modified"):
            await github_adapter_with_mock_client.put_file(
                github_repo_ref,
                path="docs/existing.md",
                content="updated body",
                message="update existing.md",
                branch="main",
            )

    @pytest.mark.asyncio
    async def test_unrelated_422_propagates_without_conflict_mislabel(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """A 422 for a reason unrelated to a stale sha (e.g. branch protection)
        must not be mislabeled as a retryable ConflictError."""
        mock_github_http_client.get_file_contents = AsyncMock(
            return_value={"content": "b2xk", "sha": "current-sha"}
        )
        response = httpx.Response(
            422,
            json={"message": "Changes must be made through a pull request"},
            request=httpx.Request("PUT", "https://api.github.com/contents"),
        )
        error = httpx.HTTPStatusError("unprocessable", request=response.request, response=response)
        mock_github_http_client.create_or_update_file = AsyncMock(side_effect=error)

        with pytest.raises(httpx.HTTPStatusError):
            await github_adapter_with_mock_client.put_file(
                github_repo_ref,
                path="docs/existing.md",
                content="updated body",
                message="update existing.md",
                branch="main",
            )


class TestParseWebhookEnrichment:
    @pytest.mark.asyncio
    async def test_issue_comment_on_pr_populates_change_request_and_comment(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        """An issue_comment event on a PR carries the PR's identity under
        payload["issue"]["pull_request"], not top-level payload["pull_request"] —
        this must still populate change_request, plus the comment field."""
        payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "title": "Add feature",
                "body": "PR description",
                "state": "open",
                "html_url": "https://github.com/test/repo/issues/42",
                "pull_request": {"url": "https://api.github.com/repos/test/repo/pulls/42"},
            },
            "comment": {
                "id": 555,
                "body": "Looks good",
                "user": {"login": "reviewer1"},
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "reviewer1", "type": "User"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "id-1"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.kind == EventKind.COMMENT_CREATED
        assert event.change_request is not None
        assert event.change_request.identity.native_id == 42
        assert event.change_request.title == "Add feature"
        assert event.comment is not None
        assert event.comment.id == "555"
        assert event.comment.body == "Looks good"
        assert event.comment.author == "reviewer1"

    @pytest.mark.asyncio
    async def test_issue_comment_on_plain_issue_has_no_change_request(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        """An issue_comment on a plain issue (no "pull_request" key under
        "issue") must not fabricate a change_request."""
        payload = {
            "action": "created",
            "issue": {"number": 7, "title": "Bug report", "state": "open"},
            "comment": {"id": 1, "body": "confirmed", "user": {"login": "x"}},
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "x", "type": "User"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "id-2"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.change_request is None
        assert event.comment is not None

    @pytest.mark.asyncio
    async def test_pull_request_review_comment_populates_comment_with_path_line(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        payload = {
            "action": "created",
            "comment": {
                "id": 999,
                "body": "nit: rename this",
                "user": {"login": "reviewer2"},
                "path": "src/foo.py",
                "line": 42,
                "in_reply_to_id": 888,
            },
            "pull_request": {
                "number": 10,
                "html_url": "https://github.com/test/repo/pull/10",
                "title": "Fix bug",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "fix"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "reviewer2", "type": "User"},
        }
        body = json.dumps(payload).encode()
        headers = {
            "X-GitHub-Event": "pull_request_review_comment",
            "X-GitHub-Delivery": "id-3",
        }
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.comment is not None
        assert event.comment.path == "src/foo.py"
        assert event.comment.line == 42
        assert event.comment.in_reply_to == "888"

    @pytest.mark.asyncio
    async def test_pull_request_review_populates_review(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        payload = {
            "action": "submitted",
            "review": {
                "id": 321,
                "state": "changes_requested",
                "body": "please fix",
                "user": {"login": "reviewer3"},
            },
            "pull_request": {
                "number": 11,
                "html_url": "https://github.com/test/repo/pull/11",
                "title": "Add thing",
                "body": "",
                "state": "open",
                "draft": False,
                "head": {"ref": "add-thing"},
                "base": {"ref": "main"},
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "reviewer3", "type": "User"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "pull_request_review", "X-GitHub-Delivery": "id-4"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.review is not None
        assert event.review.id == "321"
        assert event.review.state == ReviewState.CHANGES_REQUESTED
        assert event.review.author == "reviewer3"

    @pytest.mark.asyncio
    async def test_check_run_populates_check(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        payload = {
            "action": "completed",
            "check_run": {
                "id": 654,  # check-run id, distinct from the workflow run id
                "name": "build",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/test/repo/runs/654",
                "details_url": "https://github.com/test/repo/actions/runs/987",
                "app": {"slug": "github-actions"},
                "pull_requests": [{"number": 12}],
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "github-actions[bot]", "type": "Bot"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "id-5"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.check is not None
        assert event.check.name == "build"
        assert event.check.conclusion == CheckConclusion.FAILURE
        # logs_url holds the workflow run id (987), not the check-run id (654).
        assert event.check.logs_url == "987"
        # check_run.pull_requests[] is a "simple pull request" stub (number/
        # head/base only) -- without this, a real check_run webhook could
        # never be matched to its implementation PR downstream.
        assert event.change_request is not None
        assert event.change_request.identity.native_id == 12

    @pytest.mark.asyncio
    async def test_check_run_falls_back_to_nested_check_suite_pull_requests(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        """Some check_run payloads carry the PR list only on the nested
        check_suite object, not on check_run itself."""
        payload = {
            "action": "completed",
            "check_run": {
                "id": 654,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "github-actions"},
                "check_suite": {"pull_requests": [{"number": 34}]},
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "github-actions[bot]", "type": "Bot"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "id-5b"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.change_request is not None
        assert event.change_request.identity.native_id == 34

    @pytest.mark.asyncio
    async def test_check_suite_has_no_single_check(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        """A check_suite bundles many check runs; it doesn't map onto the
        single optional CheckRun NormalizedEvent.check carries. Leave it None
        -- callers needing the full set call get_checks()."""
        payload = {
            "action": "completed",
            "check_suite": {
                "id": 1,
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [{"number": 56}],
            },
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "github-actions[bot]", "type": "Bot"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "check_suite", "X-GitHub-Delivery": "id-6"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.check is None
        assert event.kind == EventKind.CHECK_UPDATED
        # check_suite.pull_requests[] identifies the PR even though this
        # event kind never populates .check (see docstring above).
        assert event.change_request is not None
        assert event.change_request.identity.native_id == 56

    @pytest.mark.asyncio
    async def test_check_event_without_pull_requests_leaves_change_request_none(
        self,
        github_adapter: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        github_connection: Connection,
    ):
        """A check event for a commit with no associated PR (e.g. a push to
        main) has no pull_requests stub at all -- change_request stays None
        rather than raising."""
        payload = {
            "action": "completed",
            "check_suite": {"id": 1, "status": "completed", "pull_requests": []},
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "github-actions[bot]", "type": "Bot"},
        }
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "check_suite", "X-GitHub-Delivery": "id-6b"}
        resolver = MockResolver(github_repo_ref, github_connection)

        event = await github_adapter.parse_webhook(headers, body, resolver)

        assert event.change_request is None


class TestCreateBranch:
    @pytest.mark.asyncio
    async def test_delegates_to_client(
        self,
        github_adapter_with_mock_client: GitHubAdapter,
        github_repo_ref: RepositoryRef,
        mock_github_http_client: GitHubClient,
    ):
        """create_branch splits the namespace and delegates, discarding the
        client's return value (the protocol returns None)."""
        mock_github_http_client.create_branch = AsyncMock(
            return_value={"ref": "refs/heads/forge/feature", "object": {"sha": "deadbeef"}}
        )

        result = await github_adapter_with_mock_client.create_branch(
            github_repo_ref, name="forge/feature", base="main"
        )

        mock_github_http_client.create_branch.assert_awaited_once_with(
            owner="test", repo="repo", branch_name="forge/feature", base="main"
        )
        assert result is None
