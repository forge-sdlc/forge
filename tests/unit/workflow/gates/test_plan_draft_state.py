"""Regression coverage for state-backed epic draft provisioning."""

from datetime import UTC, datetime
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
    jira.add_attachment.assert_not_awaited()
    jira.delete_attachments_by_name.assert_not_awaited()
