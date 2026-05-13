"""implement_task harvests handoff.md into state after container success."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def task_state(tmp_path):
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "handoff.md").write_text("task 1 done")
    return {
        "ticket_key": "TEST-1",
        "workspace_path": str(tmp_path),
        "current_repo": "org/repo",
        "current_task_key": "TEST-2",
        "task_keys": ["TEST-2"],
        "tasks_by_repo": {"org/repo": ["TEST-2"]},
        "implemented_tasks": [],
        "context": {"branch_name": "forge/test-1", "guardrails": ""},
        "fork_owner": "forge-bot",
        "fork_repo": "repo",
        "is_paused": False,
        "retry_count": 0,
        "forge_artifacts": {},
    }


@pytest.mark.asyncio
async def test_implement_task_harvests_handoff_on_success(task_state, tmp_path):
    from forge.workflow.nodes.implementation import implement_task

    mock_jira = AsyncMock()
    mock_jira.get_issue.return_value = MagicMock(
        summary="Fix the thing", description="description"
    )
    mock_jira.close = AsyncMock()

    mock_runner = AsyncMock()
    mock_runner.run.return_value = MagicMock(success=True)

    mock_git = MagicMock()

    with (
        patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
        patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        patch(
            "forge.workflow.nodes.implementation.prepare_workspace",
            return_value=(str(tmp_path), mock_git),
        ),
    ):
        result = await implement_task(task_state)

    assert result["forge_artifacts"]["org/repo"]["handoff.md"] == "task 1 done"


@pytest.mark.asyncio
async def test_implement_task_does_not_harvest_on_failure(task_state, tmp_path):
    from forge.workflow.nodes.implementation import implement_task

    mock_jira = AsyncMock()
    mock_jira.get_issue.return_value = MagicMock(
        summary="Fix the thing", description="description"
    )
    mock_jira.close = AsyncMock()

    mock_runner = AsyncMock()
    mock_runner.run.return_value = MagicMock(
        success=False, error_message="container failed"
    )

    mock_git = MagicMock()

    with (
        patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
        patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=mock_runner),
        patch(
            "forge.workflow.nodes.implementation.prepare_workspace",
            return_value=(str(tmp_path), mock_git),
        ),
        patch("forge.workflow.nodes.error_handler.notify_error", new_callable=AsyncMock),
    ):
        result = await implement_task(task_state)

    assert result.get("forge_artifacts", {}).get("org/repo", {}) == {}
