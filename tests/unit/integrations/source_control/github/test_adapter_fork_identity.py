from unittest.mock import AsyncMock

import pytest

from forge.integrations.source_control.contracts import (
    Connection,
    Provider,
    RepositoryRef,
    WriteTarget,
)
from forge.integrations.source_control.github.adapter import GitHubAdapter


def _repo_ref(mode="fork"):
    return RepositoryRef(
        id="acme/widgets",
        provider=Provider.GITHUB,
        connection="c",
        namespace="acme/widgets",
        default_branch="main",
        change_request_mode=mode,
    )


def _conn():
    return Connection(
        name="c",
        provider=Provider.GITHUB,
        base_url="",
        credential_env="GITHUB_TOKEN",
        webhook_secret_env="",
    )


@pytest.mark.asyncio
async def test_ensure_write_target_exposes_fork_identity():
    client = AsyncMock()
    client.get_or_create_fork.return_value = {
        "owner": {"login": "forkuser"},
        "name": "widgets",
        "clone_url": "https://github.com/forkuser/widgets.git",
    }
    client.sync_fork_with_upstream.return_value = True
    adapter = GitHubAdapter(connection=_conn(), client=client)

    target = await adapter.ensure_write_target(_repo_ref())

    assert target.fork_owner == "forkuser"
    assert target.fork_repo == "widgets"


@pytest.mark.asyncio
async def test_direct_mode_has_no_fork_identity():
    adapter = GitHubAdapter(connection=_conn(), client=AsyncMock())
    target = await adapter.ensure_write_target(_repo_ref(mode="direct"))
    assert target.fork_owner is None and target.fork_repo is None


@pytest.mark.asyncio
async def test_create_change_request_uses_forkowner_colon_branch_head():
    client = AsyncMock()
    client.create_pull_request.return_value = type(
        "R",
        (),
        {
            "pr": {
                "number": 7,
                "html_url": "u",
                "title": "t",
                "body": "",
                "state": "open",
                "head": {"ref": "feature/x"},
                "base": {"ref": "main"},
                "draft": False,
            },
            "created": True,
        },
    )()
    adapter = GitHubAdapter(connection=_conn(), client=client)
    target = WriteTarget(
        clone_url="",
        push_remote_name="origin",
        head_ref="feature/x",
        base_branch="main",
        fork_owner="forkuser",
        fork_repo="widgets",
    )

    await adapter.create_change_request(_repo_ref(), target, title="t", body="b")

    _, kwargs = client.create_pull_request.call_args
    assert kwargs["head"] == "forkuser:feature/x"


@pytest.mark.asyncio
async def test_create_change_request_propagates_created_false_for_existing_pr():
    client = AsyncMock()
    client.create_pull_request.return_value = type(
        "R",
        (),
        {
            "pr": {
                "number": 7,
                "html_url": "u",
                "title": "t",
                "body": "",
                "state": "open",
                "head": {"ref": "feature/x"},
                "base": {"ref": "main"},
                "draft": False,
            },
            "created": False,
        },
    )()
    adapter = GitHubAdapter(connection=_conn(), client=client)
    target = WriteTarget(
        clone_url="",
        push_remote_name="origin",
        head_ref="feature/x",
        base_branch="main",
        fork_owner="forkuser",
        fork_repo="widgets",
    )

    change_request = await adapter.create_change_request(_repo_ref(), target, title="t", body="b")

    assert change_request.created is False
