"""Tests for the generic task-first implementation node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.implementation_input import (
    NoPendingImplementationWork,
    ResolvedImplementationInput,
)
from forge.workflow.nodes.implement_work import implement_work


def resolved_task() -> ResolvedImplementationInput:
    artifact = {
        "id": "jira:TASK-1:task",
        "kind": "task",
        "source": "TASK-1",
        "content": "Implement the endpoint",
        "repository": "acme/api",
        "digest": "sha256:task",
    }
    return ResolvedImplementationInput(
        work_unit={
            "id": "TASK-1",
            "kind": "task",
            "key": "TASK-1",
            "repository": "acme/api",
            "status": "pending",
            "source_artifact_ids": [artifact["id"]],
        },
        context_artifacts=(artifact,),
        instructions=artifact["content"],
        summary="Implement endpoint",
    )


@pytest.mark.asyncio
async def test_implements_resolved_task_and_marks_normalized_work_complete() -> None:
    jira = MagicMock()
    jira.close = AsyncMock()
    git = MagicMock()

    async def execute(state, *_args, **_kwargs):
        return {**state, "last_error": None, "commit_info": {"committed": True}}

    with (
        patch("forge.workflow.nodes.implement_work.JiraClient", return_value=jira),
            patch(
                "forge.workflow.nodes.implement_work.prepare_workspace",
                AsyncMock(return_value=("/tmp/ws", git)),
        ),
        patch(
            "forge.workflow.nodes.implement_work.resolve_implementation_input",
            AsyncMock(return_value=resolved_task()),
        ),
        patch(
            "forge.workflow.nodes.implement_work.fetch_and_inject_references",
            AsyncMock(side_effect=lambda _state, _jira, prompt: prompt),
        ),
        patch("forge.workflow.nodes.implement_work.post_status_comment", AsyncMock()),
        patch(
            "forge.workflow.nodes.implement_work.run_and_persist_execution",
            AsyncMock(side_effect=execute),
        ),
    ):
        result = await implement_work(
            {"ticket_key": "FEAT-1", "current_repo": "acme/api", "implemented_tasks": []}
        )

    assert result["implemented_tasks"] == ["TASK-1"]
    assert result["work_units"][0]["status"] == "completed"
    assert result["work_resolution"]["strategy"] == "task_first"
    assert result["current_node"] == "implement_work"
    jira.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_pending_work_routes_to_local_review() -> None:
    jira = MagicMock()
    jira.close = AsyncMock()
    with (
        patch("forge.workflow.nodes.implement_work.JiraClient", return_value=jira),
            patch(
                "forge.workflow.nodes.implement_work.prepare_workspace",
                AsyncMock(return_value=("/tmp/ws", MagicMock())),
        ),
        patch(
            "forge.workflow.nodes.implement_work.resolve_implementation_input",
            AsyncMock(side_effect=NoPendingImplementationWork("complete")),
        ),
    ):
        result = await implement_work({"ticket_key": "FEAT-1", "current_repo": "acme/api"})

    assert result["current_node"] == "local_review"
    assert result["last_error"] is None
