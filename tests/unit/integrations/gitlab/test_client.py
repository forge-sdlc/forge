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


class TestNotesAndDiscussions:
    @pytest.mark.asyncio
    async def test_create_note(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": 1, "body": "hi"}
        client._client.post = AsyncMock(return_value=response)

        result = await client.create_note("test/repo", 42, "hi")

        client._client.post.assert_awaited_once_with(
            "/projects/test%2Frepo/merge_requests/42/notes", json={"body": "hi"}
        )
        assert result["id"] == 1

    @pytest.mark.asyncio
    async def test_get_discussions(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = [{"id": "abc"}]
        client._client.get = AsyncMock(return_value=response)

        result = await client.get_discussions("test/repo", 42)

        client._client.get.assert_awaited_once_with(
            "/projects/test%2Frepo/merge_requests/42/discussions"
        )
        assert result == [{"id": "abc"}]

    @pytest.mark.asyncio
    async def test_reply_to_discussion(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": 2, "body": "reply"}
        client._client.post = AsyncMock(return_value=response)

        await client.reply_to_discussion("test/repo", 42, "abc", "reply")

        client._client.post.assert_awaited_once_with(
            "/projects/test%2Frepo/merge_requests/42/discussions/abc/notes", json={"body": "reply"}
        )

    @pytest.mark.asyncio
    async def test_get_approvals(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"approved_by": [{"user": {"id": 1, "username": "alice"}}]}
        client._client.get = AsyncMock(return_value=response)

        result = await client.get_approvals("test/repo", 42)

        client._client.get.assert_awaited_once_with(
            "/projects/test%2Frepo/merge_requests/42/approvals"
        )
        assert result["approved_by"][0]["user"]["username"] == "alice"


class TestChecks:
    @pytest.mark.asyncio
    async def test_get_commit_statuses(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = [{"name": "build", "status": "success"}]
        client._client.get = AsyncMock(return_value=response)

        result = await client.get_commit_statuses("test/repo", "abc123")

        client._client.get.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/commits/abc123/statuses",
            params={"page": 1, "per_page": 100},
        )
        assert result[0]["name"] == "build"

    @pytest.mark.asyncio
    async def test_get_commit_statuses_paginates_until_short_page(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False

        page1_items = [{"name": f"job{i}", "status": "success"} for i in range(100)]
        page2_items = [{"name": "job100", "status": "success"}]

        page1_response = MagicMock()
        page1_response.raise_for_status = MagicMock()
        page1_response.json.return_value = page1_items

        page2_response = MagicMock()
        page2_response.raise_for_status = MagicMock()
        page2_response.json.return_value = page2_items

        client._client.get = AsyncMock(side_effect=[page1_response, page2_response])

        result = await client.get_commit_statuses("test/repo", "abc123")

        assert result == page1_items + page2_items
        assert client._client.get.await_count == 2
        client._client.get.assert_any_await(
            "/projects/test%2Frepo/repository/commits/abc123/statuses",
            params={"page": 1, "per_page": 100},
        )
        client._client.get.assert_any_await(
            "/projects/test%2Frepo/repository/commits/abc123/statuses",
            params={"page": 2, "per_page": 100},
        )

    @pytest.mark.asyncio
    async def test_get_job_trace_returns_raw_text(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.text = "line1\nline2\n"
        client._client.get = AsyncMock(return_value=response)

        logs = await client.get_job_trace("test/repo", 987654)

        client._client.get.assert_awaited_once_with("/projects/test%2Frepo/jobs/987654/trace")
        assert logs == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_get_job_artifacts_returns_bytes(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.content = b"PK\x03\x04zipbytes"
        client._client.get = AsyncMock(return_value=response)

        artifacts = await client.get_job_artifacts("test/repo", 987654)

        assert artifacts == b"PK\x03\x04zipbytes"

    @pytest.mark.asyncio
    async def test_get_job_artifacts_returns_none_on_404(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 404
        client._client.get = AsyncMock(return_value=response)

        artifacts = await client.get_job_artifacts("test/repo", 987654)

        assert artifacts is None


class TestFileOperations:
    @pytest.mark.asyncio
    async def test_get_file_raw_returns_text_on_200(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.text = "print('hi')\n"
        client._client.get = AsyncMock(return_value=response)

        content = await client.get_file_raw("test/repo", "src/x.py", "main")

        client._client.get.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/files/src%2Fx.py/raw", params={"ref": "main"}
        )
        assert content == "print('hi')\n"

    @pytest.mark.asyncio
    async def test_get_file_raw_returns_none_on_404(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 404
        client._client.get = AsyncMock(return_value=response)

        assert await client.get_file_raw("test/repo", "missing.py", "main") is None

    @pytest.mark.asyncio
    async def test_get_file_metadata_returns_last_commit_id(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.headers = {"X-Gitlab-Last-Commit-Id": "deadbeef"}
        client._client.head = AsyncMock(return_value=response)

        metadata = await client.get_file_metadata("test/repo", "src/x.py", "main")

        client._client.head.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/files/src%2Fx.py", params={"ref": "main"}
        )
        client._client.get.assert_not_called()
        assert metadata["last_commit_id"] == "deadbeef"

    @pytest.mark.asyncio
    async def test_get_file_metadata_returns_none_on_404(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.status_code = 404
        client._client.head = AsyncMock(return_value=response)

        assert await client.get_file_metadata("test/repo", "missing.py", "main") is None

    @pytest.mark.asyncio
    async def test_create_file(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=response)

        await client.create_file(
            "test/repo", "new.py", branch="main", content="x = 1", commit_message="add file"
        )

        client._client.post.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/files/new.py",
            json={"branch": "main", "content": "x = 1", "commit_message": "add file"},
        )

    @pytest.mark.asyncio
    async def test_update_file_passes_last_commit_id(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        client._client.put = AsyncMock(return_value=response)

        await client.update_file(
            "test/repo",
            "existing.py",
            branch="main",
            content="x = 2",
            commit_message="update",
            last_commit_id="deadbeef",
        )

        client._client.put.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/files/existing.py",
            json={
                "branch": "main",
                "content": "x = 2",
                "commit_message": "update",
                "last_commit_id": "deadbeef",
            },
        )

    @pytest.mark.asyncio
    async def test_create_branch(self):
        client = GitLabClient(credential="tok")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.is_closed = False
        response = MagicMock()
        response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=response)

        await client.create_branch("test/repo", "feature", "main")

        client._client.post.assert_awaited_once_with(
            "/projects/test%2Frepo/repository/branches",
            json={"branch": "feature", "ref": "main"},
        )
