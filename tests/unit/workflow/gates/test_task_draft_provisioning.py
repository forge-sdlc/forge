"""Regression coverage for task-draft repository resolution."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forge.models.draft import DraftItem, ForgeDecompositionDraft
from forge.workflow.gates.task_approval import provision_tasks_from_draft


def _draft(repo: str = "unknown") -> ForgeDecompositionDraft:
    return ForgeDecompositionDraft(
        parent_key="AISOS-1",
        phase="tasks",
        items=[
            DraftItem(
                id=1,
                summary="Implement repository handling",
                description="Implement the repository handling.",
                repo=repo,
                acceptance_criteria=[],
                epic_key="AISOS-2",
            )
        ],
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_task_draft_inherits_repo_label_from_parent_epic() -> None:
    jira = AsyncMock()
    jira.search_issues.return_value = []
    jira.get_issue.return_value = SimpleNamespace(project_key="AISOS")
    jira.get_labels.return_value = ["repo:forge-sdlc/forge"]
    jira.create_task.return_value = "AISOS-3"

    keys, by_repo = await provision_tasks_from_draft(
        {"ticket_key": "AISOS-1", "tasks_draft": _draft()}, jira
    )

    assert keys == ["AISOS-3"]
    assert by_repo == {"forge-sdlc/forge": ["AISOS-3"]}
    assert "repo:forge-sdlc/forge" in jira.create_task.await_args.kwargs["labels"]
    jira.get_project_default_repo.assert_not_awaited()
    jira.add_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_draft_without_task_or_epic_repo_is_reported_and_not_routed() -> None:
    jira = AsyncMock()
    jira.search_issues.return_value = []
    jira.get_issue.return_value = SimpleNamespace(project_key="AISOS")
    jira.get_labels.return_value = []
    jira.create_task.return_value = "AISOS-3"

    keys, by_repo = await provision_tasks_from_draft(
        {"ticket_key": "AISOS-1", "tasks_draft": _draft()}, jira
    )

    assert keys == ["AISOS-3"]
    assert by_repo == {}
    assert not any(label.startswith("repo:") for label in jira.create_task.await_args.kwargs["labels"])
    assert "parent Epic AISOS-2" in jira.add_comment.await_args.args[1]
    jira.get_project_default_repo.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_unlabelled_task_is_repaired_from_its_parent_epic() -> None:
    jira = AsyncMock()
    jira.search_issues.return_value = [
        SimpleNamespace(key="AISOS-3", labels=["forge:managed"], parent_key="AISOS-2")
    ]
    jira.get_labels.return_value = ["repo:forge-sdlc/forge"]

    keys, by_repo = await provision_tasks_from_draft({"ticket_key": "AISOS-1"}, jira)

    assert keys == ["AISOS-3"]
    assert by_repo == {"forge-sdlc/forge": ["AISOS-3"]}
    jira.add_labels.assert_awaited_once_with("AISOS-3", ["repo:forge-sdlc/forge"])
