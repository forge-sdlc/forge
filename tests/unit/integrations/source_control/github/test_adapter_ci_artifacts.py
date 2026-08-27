from unittest.mock import AsyncMock

import pytest

from forge.integrations.source_control.contracts import (
    CheckConclusion, CheckRun, CheckStatus, Connection, Provider, RepositoryRef,
)
from forge.integrations.source_control.github.adapter import GitHubAdapter


def _conn():
    return Connection(name="c", provider=Provider.GITHUB, base_url="",
                      credential_env="GITHUB_TOKEN", webhook_secret_env="")


def _repo_ref():
    return RepositoryRef(id="acme/widgets", provider=Provider.GITHUB, connection="c",
                         namespace="acme/widgets", default_branch="main",
                         change_request_mode="fork")


def test_map_check_run_carries_output():
    adapter = GitHubAdapter(connection=_conn(), client=AsyncMock())
    entry = {"name": "pytest", "status": "completed", "conclusion": "failure",
             "html_url": "h", "app": {"slug": "github-actions"},
             "details_url": "https://github.com/o/r/actions/runs/55",
             "output": {"title": "T", "summary": "S", "text": "boom"}}
    check = adapter._map_check_run(entry)
    assert check.output == {"title": "T", "summary": "S", "text": "boom"}


@pytest.mark.asyncio
async def test_get_check_artifacts_returns_named_zips():
    client = AsyncMock()
    client.get_run_artifacts.return_value = [{"id": 1, "name": "logs"}]
    client.download_artifact_zip.return_value = b"PK\x03\x04zip"
    adapter = GitHubAdapter(connection=_conn(), client=client)
    check = CheckRun(name="pytest", status=CheckStatus.COMPLETED,
                     conclusion=CheckConclusion.FAILURE, logs_url="55")

    artifacts = await adapter.get_check_artifacts(_repo_ref(), check)

    assert artifacts == [("logs", b"PK\x03\x04zip")]
    client.get_run_artifacts.assert_awaited_once_with("acme", "widgets", 55)


@pytest.mark.asyncio
async def test_get_check_artifacts_empty_when_no_run():
    adapter = GitHubAdapter(connection=_conn(), client=AsyncMock())
    check = CheckRun(name="prow", status=CheckStatus.COMPLETED,
                     conclusion=CheckConclusion.FAILURE, logs_url=None)
    assert await adapter.get_check_artifacts(_repo_ref(), check) == []
