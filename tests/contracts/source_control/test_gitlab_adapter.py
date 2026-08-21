"""Tests for GitLab source control adapter."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.integrations.gitlab.client import GitLabClient
from forge.integrations.source_control.contracts import Connection, Provider, RepositoryRef
from forge.integrations.source_control.errors import (
    AuthenticationError,
    RateLimitedError,
    TransientProviderError,
)
from forge.integrations.source_control.gitlab.adapter import GitLabAdapter
from tests.contracts.source_control.conformance_suite import assert_repository_operations


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
