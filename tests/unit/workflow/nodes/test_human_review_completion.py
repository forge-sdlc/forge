"""Tests for post-merge Jira completion aggregation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import JiraStatus
from forge.workflow.nodes.human_review import (
    aggregate_epic_status,
    aggregate_feature_status,
    complete_tasks,
)


@pytest.mark.asyncio
async def test_complete_tasks_only_records_successful_jira_transitions():
    state = {
        "ticket_key": "FEAT-123",
        "implemented_tasks": ["TASK-1", "TASK-2"],
    }

    jira = MagicMock()
    jira.transition_issue = AsyncMock(side_effect=[None, RuntimeError("transition denied")])
    jira.set_workflow_label = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await complete_tasks(state)

    assert result["jira_completed_tasks"] == ["TASK-1"]


@pytest.mark.asyncio
async def test_aggregate_epic_status_does_not_mask_failed_task_transition():
    state = {
        "ticket_key": "FEAT-123",
        "implemented_tasks": ["TASK-1"],
        "jira_completed_tasks": [],
        "epic_keys": ["EPIC-1"],
    }

    jira = MagicMock()
    jira.get_epic_children = AsyncMock(
        return_value=[SimpleNamespace(key="TASK-1", status="In Progress")]
    )
    jira.transition_issue = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_epic_status(state)

    jira.transition_issue.assert_not_awaited()
    assert result["current_node"] == "complete"


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
    # The parent Epic's direct child is the Feature itself; include a done sibling
    # to model a realistic hierarchy.
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(key="FEAT-123", status="Closed"),
            SimpleNamespace(key="FEAT-SIBLING", status="Done"),
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
async def test_aggregate_feature_status_skips_parent_epic_when_no_children_found():
    """A parent Epic that returns no children (query/config mismatch) must not be closed."""
    state = {
        "ticket_key": "FEAT-123",
        "current_node": "aggregate_feature_status",
    }

    jira = MagicMock()
    mock_feature_issue = SimpleNamespace(parent_key="EPIC-PARENT")
    jira.get_issue = AsyncMock(return_value=mock_feature_issue)
    jira.get_epic_children = AsyncMock(return_value=[])
    jira.transition_issue = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_feature_status(state)

    # Feature is closed, but the parent Epic is left untouched.
    assert jira.transition_issue.call_count == 1
    jira.transition_issue.assert_called_once_with("FEAT-123", JiraStatus.CLOSED.value)
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


@pytest.mark.asyncio
async def test_aggregate_feature_status_handles_jira_search_lag():
    """Should transition Feature and parent Epic to Closed even if search returns incomplete statuses for currently-completed keys."""
    state = {
        "ticket_key": "FEAT-123",
        "implemented_tasks": ["TASK-1"],
        "jira_completed_tasks": ["TASK-1"],
        "epic_keys": ["EPIC-1"],
        "current_node": "aggregate_feature_status",
    }

    jira = MagicMock()
    mock_feature_issue = SimpleNamespace(parent_key="EPIC-PARENT")
    jira.get_issue = AsyncMock(return_value=mock_feature_issue)

    # Simulating lag where TASK-1, EPIC-1 and FEAT-123 are returned with old status
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(key="TASK-1", status="In Progress"),  # In implemented_tasks
            SimpleNamespace(key="FEAT-123", status="Under Review"),  # ticket_key
            SimpleNamespace(key="EPIC-1", status="In Progress"),  # In epic_keys
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
async def test_aggregate_feature_status_handles_jira_search_lag_case_insensitive():
    """Should transition Feature and parent Epic to Closed even if search returns incomplete statuses and keys have different casing."""
    state = {
        "ticket_key": "feat-123",
        "implemented_tasks": ["task-1"],
        "jira_completed_tasks": ["task-1"],
        "epic_keys": ["epic-1"],
        "current_node": "aggregate_feature_status",
    }

    jira = MagicMock()
    mock_feature_issue = SimpleNamespace(parent_key="epic-parent")
    jira.get_issue = AsyncMock(return_value=mock_feature_issue)

    # Simulating lag where TASK-1, EPIC-1 and FEAT-123 are returned with old status and mixed casing
    jira.get_epic_children = AsyncMock(
        return_value=[
            SimpleNamespace(
                key="TASK-1", status="In Progress"
            ),  # In implemented_tasks (lowercase in state)
            SimpleNamespace(
                key="feat-123", status="Under Review"
            ),  # ticket_key (lowercase in state)
            SimpleNamespace(
                key="Epic-1", status="In Progress"
            ),  # In epic_keys (lowercase in state)
        ]
    )
    jira.transition_issue = AsyncMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    with patch("forge.workflow.nodes.human_review.JiraClient", return_value=jira):
        result = await aggregate_feature_status(state)

    # Asserts that transition_issue is called on the Feature key and the parent key
    assert jira.transition_issue.call_count == 2
    jira.transition_issue.assert_any_call("feat-123", JiraStatus.CLOSED.value)
    jira.transition_issue.assert_any_call("epic-parent", JiraStatus.CLOSED.value)

    # Asserts that the updated state has feature_completed=True and current_node="complete"
    assert result["feature_completed"] is True
    assert result["current_node"] == "complete"
