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
