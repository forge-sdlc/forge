from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from forge.integrations.gitlab.client import GitLabClient


def test_default_base_url_is_gitlab_com():
    client = GitLabClient(credential="tok")
    assert client.base_url == "https://gitlab.com/api/v4"


def test_custom_base_url_and_ca_path_are_respected():
    client = GitLabClient(
        credential="tok", base_url="https://gitlab.example.com/api/v4", ca_path="/ca.pem"
    )
    assert client.base_url == "https://gitlab.example.com/api/v4"
    assert client._ca_path == "/ca.pem"


@pytest.mark.asyncio
async def test_get_client_sends_private_token_header():
    client = GitLabClient(credential="glpat-secret")
    http_client = await client._get_client()
    assert http_client.headers["PRIVATE-TOKEN"] == "glpat-secret"


@pytest.mark.asyncio
async def test_get_project_encodes_namespace_and_returns_json():
    client = GitLabClient(credential="tok")
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    response = MagicMock()
    response.json.return_value = {"id": 42, "path_with_namespace": "group/sub/proj"}
    response.raise_for_status = MagicMock()
    client._client.get = AsyncMock(return_value=response)

    result = await client.get_project("group/sub/proj")

    client._client.get.assert_awaited_once_with("/projects/group%2Fsub%2Fproj")
    assert result["id"] == 42


@pytest.mark.asyncio
async def test_get_authenticated_user():
    client = GitLabClient(credential="tok")
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._client.is_closed = False
    response = MagicMock()
    response.json.return_value = {"username": "octocat-gl"}
    response.raise_for_status = MagicMock()
    client._client.get = AsyncMock(return_value=response)

    result = await client.get_authenticated_user()

    client._client.get.assert_awaited_once_with("/user")
    assert result["username"] == "octocat-gl"


@pytest.mark.asyncio
async def test_close_closes_the_http_client():
    client = GitLabClient(credential="tok")
    await client._get_client()
    mock_aclose = AsyncMock()
    client._client.aclose = mock_aclose

    await client.close()

    mock_aclose.assert_awaited_once()


class TestGetOrCreateFork:
    @pytest.mark.asyncio
    async def test_returns_existing_fork_without_creating(self):
        client = GitLabClient(credential="tok")
        client.get_fork = AsyncMock(return_value={"id": 7, "import_status": "finished"})
        client.create_fork = AsyncMock()

        fork = await client.get_or_create_fork("upstream/repo", fork_owner="forge-bot")

        client.create_fork.assert_not_awaited()
        assert fork["id"] == 7

    @pytest.mark.asyncio
    async def test_creates_and_waits_for_import_finished(self, monkeypatch):
        client = GitLabClient(credential="tok")
        client.get_fork = AsyncMock(return_value=None)
        client.create_fork = AsyncMock(return_value={"id": 9, "import_status": "scheduled"})
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False

        poll_responses = [
            {"id": 9, "import_status": "started"},
            {"id": 9, "import_status": "finished"},
        ]

        async def _fake_get(_path, **_kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json.return_value = poll_responses.pop(0)
            return response

        client._client.get = AsyncMock(side_effect=_fake_get)
        sleeps = []
        monkeypatch.setattr(
            "forge.integrations.gitlab.client.asyncio.sleep",
            AsyncMock(side_effect=lambda s: sleeps.append(s)),
        )

        fork = await client.get_or_create_fork("upstream/repo", fork_owner="forge-bot")

        assert fork["import_status"] == "finished"
        assert sleeps == [2, 2]


class TestCreateMergeRequest:
    @pytest.mark.asyncio
    async def test_creates_mr_in_source_project(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"iid": 5, "title": "Test"}
        client._client.post = AsyncMock(return_value=response)

        result = await client.create_merge_request(
            "forge-bot/repo",
            source_branch="feature",
            target_branch="main",
            title="Test",
            description="body",
            target_project_id=100,
        )

        client._client.post.assert_awaited_once_with(
            "/projects/forge-bot%2Frepo/merge_requests",
            json={
                "source_branch": "feature",
                "target_branch": "main",
                "title": "Test",
                "description": "body",
                "target_project_id": 100,
            },
        )
        assert result["iid"] == 5
