"""Tests for post-merge Jira completion aggregation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import JiraStatus
from forge.workflow.nodes.human_review import aggregate_epic_status, aggregate_feature_status


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


@pytest.mark.asyncio
async def test_aggregate_feature_status_transitions_parent_epic():
    """Should transition Feature and its parent Epic to Closed/Done status."""
    state = {
        "ticket_key": "FEAT-123",
        "current_node": "aggregate_feature_status",
    }

    jira = MagicMock()
    mock_feature_issue = SimpleNamespace(parent_key="EPIC-PARENT")
    jira.get_issue = AsyncMock(return_value=mock_feature_issue)
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(key="TASK-1", status="Closed"),
            SimpleNamespace(key="TASK-2", status="Done"),
        ]
    )
    jira.transition_issue = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_feature_status(state)

    # Asserts that transition_issue is called on the Feature key and the parent key
    assert jira.transition_issue.call_count == 2
    jira.transition_issue.assert_any_call("FEAT-123", JiraStatus.CLOSED.value)
    jira.transition_issue.assert_any_call("EPIC-PARENT", JiraStatus.CLOSED.value)

    # Asserts that the updated state has feature_completed=True and current_node="complete"
    assert result["feature_completed"] is True
    assert result["current_node"] == "complete"


@pytest.mark.asyncio
async def test_aggregate_feature_status_skips_parent_epic_if_incomplete_children():
    """Should transition Feature but NOT its parent Epic if some child tickets are incomplete."""
    state = {
        "ticket_key": "FEAT-123",
        "current_node": "aggregate_feature_status",
    }

    jira = MagicMock()
    mock_feature_issue = SimpleNamespace(parent_key="EPIC-PARENT")
    jira.get_issue = AsyncMock(return_value=mock_feature_issue)
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(key="TASK-1", status="Closed"),
            SimpleNamespace(key="TASK-2", status="In Progress"),
        ]
    )
    jira.transition_issue = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_feature_status(state)

    # Asserts that transition_issue is called ONLY on the Feature key
    assert jira.transition_issue.call_count == 1
    jira.transition_issue.assert_called_once_with("FEAT-123", JiraStatus.CLOSED.value)

    # Asserts that the updated state has feature_completed=True and current_node="complete"
    assert result["feature_completed"] is True
    assert result["current_node"] == "complete"
