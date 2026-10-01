"""Regression coverage for state-backed epic draft provisioning."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forge.models.draft import DraftItem, ForgeDecompositionDraft
from forge.workflow.gates.plan_approval import provision_epics_from_draft


@pytest.mark.asyncio
async def test_provision_epics_uses_checkpointed_draft_without_attachment_lifecycle() -> None:
    draft = ForgeDecompositionDraft(
        parent_key="AISOS-1",
        phase="epics",
        items=[
            DraftItem(
                id=1,
                summary="Small change",
                description="Implement the small change.",
                repo="forge-sdlc/forge",
                acceptance_criteria=[],
            )
        ],
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    jira = AsyncMock()
    jira.search_issues.return_value = []
    jira.get_issue.return_value.project_key = "AISOS"
    jira.create_epic.return_value = "AISOS-2"

    epic_keys = await provision_epics_from_draft(
        {"ticket_key": "AISOS-1", "plan_draft": draft}, jira
    )

    assert epic_keys == ["AISOS-2"]
    jira.create_epic.assert_awaited_once()
    assert "forge:epic-item:AISOS-1:1" in jira.create_epic.call_args.kwargs["labels"]
    assert jira.search_issues.call_args.kwargs["max_results"] is None
    jira.add_attachment.assert_not_awaited()
    jira.delete_attachments_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_epics_resumes_after_one_epic_was_created() -> None:
    first_summary = "A" * 294
    draft = ForgeDecompositionDraft(
        parent_key="AISOS-1",
        phase="epics",
        items=[
            DraftItem(
                id=1,
                summary=first_summary,
                description="First",
                repo="forge-sdlc/forge",
                acceptance_criteria=[],
            ),
            DraftItem(
                id=2,
                summary="Second",
                description="Second",
                repo="forge-sdlc/forge",
                acceptance_criteria=[],
            ),
        ],
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    jira = AsyncMock()
    jira.search_issues.return_value = [SimpleNamespace(key="AISOS-2", summary=first_summary[:255])]
    jira.get_issue.return_value.project_key = "AISOS"
    jira.create_epic.return_value = "AISOS-3"

    epic_keys = await provision_epics_from_draft(
        {"ticket_key": "AISOS-1", "plan_draft": draft}, jira
    )

    assert epic_keys == ["AISOS-2", "AISOS-3"]
    jira.create_epic.assert_awaited_once()
    jira.add_labels.assert_awaited_once_with("AISOS-2", ["forge:epic-item:AISOS-1:1"])
    assert jira.create_epic.call_args.kwargs["summary"] == "Second"


@pytest.mark.asyncio
async def test_provision_epics_reuses_marked_epic_after_title_change() -> None:
    draft = ForgeDecompositionDraft(
        parent_key="AISOS-1",
        phase="epics",
        items=[
            DraftItem(
                id=1,
                summary="Updated title",
                description="Updated description",
                repo="forge-sdlc/forge",
                acceptance_criteria=[],
            )
        ],
        version=2,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    jira = AsyncMock()
    jira.search_issues.return_value = [
        SimpleNamespace(
            key="AISOS-2",
            summary="Old title",
            labels=["forge:parent:AISOS-1", "forge:epic-item:AISOS-1:1"],
        )
    ]
    jira.get_issue.return_value.project_key = "AISOS"

    assert await provision_epics_from_draft(
        {"ticket_key": "AISOS-1", "plan_draft": draft}, jira
    ) == ["AISOS-2"]
    jira.create_epic.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_epics_stops_on_unmatched_legacy_epic() -> None:
    draft = ForgeDecompositionDraft(
        parent_key="AISOS-1",
        phase="epics",
        items=[
            DraftItem(
                id=1,
                summary="Updated title",
                description="Updated description",
                repo="forge-sdlc/forge",
                acceptance_criteria=[],
            )
        ],
        version=2,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    jira = AsyncMock()
    jira.search_issues.return_value = [SimpleNamespace(key="AISOS-2", summary="Old title")]
    jira.get_issue.return_value.project_key = "AISOS"

    with pytest.raises(ValueError, match="Cannot safely reconcile unmarked Epics"):
        await provision_epics_from_draft({"ticket_key": "AISOS-1", "plan_draft": draft}, jira)
    jira.create_epic.assert_not_awaited()
