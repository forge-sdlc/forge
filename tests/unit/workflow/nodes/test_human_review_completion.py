"""Tests for post-merge Jira completion aggregation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import JiraStatus
from forge.workflow.nodes.human_review import aggregate_epic_status


@pytest.mark.asyncio
async def test_aggregate_epic_status_derives_missing_epics_from_implemented_tasks():
    """Merged workflows should close Epics even when state lost epic_keys."""
    state = {
        "ticket_key": "FEAT-123",
        "implemented_tasks": ["TASK-1", "TASK-2"],
        "epic_keys": [],
        "current_node": "aggregate_epic_status",
        "retry_count": 0,
    }

    jira = MagicMock()
    jira.get_issue = AsyncMock(
        side_effect=[
            SimpleNamespace(parent_key="EPIC-1"),
            SimpleNamespace(parent_key="EPIC-1"),
        ]
    )
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(key="TASK-1", status="Closed"),
            SimpleNamespace(key="TASK-2", status="Done"),
        ]
    )
    jira.transition_issue = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_epic_status(state)

    jira.transition_issue.assert_awaited_once_with("EPIC-1", JiraStatus.CLOSED.value)
    assert result["epic_keys"] == ["EPIC-1"]
    assert result["epics_completed"] is True
    assert result["current_node"] == "aggregate_feature_status"
