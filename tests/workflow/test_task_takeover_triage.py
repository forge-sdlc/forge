"""Unit and integration tests for Task Takeover triage."""

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.nodes.task_takeover_triage import triage_task
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


def make_task_state(**overrides: Any) -> TaskTakeoverState:
    """Create a TaskTakeoverState dict for triage tests."""
    state = create_initial_task_takeover_state("TASK-123")
    state_dict = cast(dict[str, Any], state)
    state_dict.update(overrides)
    return cast(TaskTakeoverState, state_dict)


@pytest.fixture
def mock_jira() -> MagicMock:
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value=MagicMock(
            summary="Login fails with special characters",
            description="Problem description",
        )
    )
    jira.get_comments = AsyncMock(return_value=[])
    jira.add_comment = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.close = AsyncMock()
    return jira


@pytest.fixture
def mock_agent() -> MagicMock:
    agent = MagicMock()
    agent.run_task = AsyncMock()
    agent.close = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_complete_ticket_passes_triage(
    mock_jira: MagicMock,
    mock_agent: MagicMock,
) -> None:
    """Verify that a complete ticket passes triage and moves to planning."""
    state = make_task_state(current_node="start")
    mock_agent.run_task.return_value = "sufficient"

    with (
        patch("forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira),
        patch("forge.workflow.stations.triage.ForgeAgent", return_value=mock_agent),
    ):
        result = await triage_task(state)

    assert result["triage_passed"] is True
    assert result["current_node"] == "generate_plan"
    assert result["is_paused"] is False
    assert result["triage_missing_fields"] == []

    # Check Jira interactions (comments go through post_status_comment which prepends emojis)
    assert mock_jira.add_comment.call_count == 2
    comments = [call.args[1] for call in mock_jira.add_comment.call_args_list]
    assert any("checking ticket completeness" in c for c in comments)
    assert any("Starting plan generation" in c for c in comments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_fields, expected_missing_list",
    [
        # Single missing section
        (["Problem Statement"], ["Problem Statement"]),
        (["Proposed Solution/Approach"], ["Proposed Solution/Approach"]),
        (["Acceptance Criteria"], ["Acceptance Criteria"]),
        # Combinations of missing sections
        (
            ["Problem Statement", "Proposed Solution/Approach"],
            ["Problem Statement", "Proposed Solution/Approach"],
        ),
        (
            ["Problem Statement", "Acceptance Criteria"],
            ["Problem Statement", "Acceptance Criteria"],
        ),
        (
            ["Proposed Solution/Approach", "Acceptance Criteria"],
            ["Proposed Solution/Approach", "Acceptance Criteria"],
        ),
        # All sections missing
        (
            ["Problem Statement", "Proposed Solution/Approach", "Acceptance Criteria"],
            ["Problem Statement", "Proposed Solution/Approach", "Acceptance Criteria"],
        ),
        # Malformed/Unexpected output fallback
        (
            "not-a-list",
            ["(could not determine — please provide additional context about the task)"],
        ),
    ],
)
async def test_incomplete_ticket_triage_permutations(
    mock_jira: MagicMock,
    mock_agent: MagicMock,
    missing_fields: Any,
    expected_missing_list: list[str],
) -> None:
    """Verify that all permutations of missing sections trigger correct state, label, and comments."""
    state = make_task_state(current_node="start")

    if isinstance(missing_fields, list):
        mock_agent.run_task.return_value = json.dumps(missing_fields)
    else:
        mock_agent.run_task.return_value = missing_fields

    with (
        patch("forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira),
        patch("forge.workflow.stations.triage.ForgeAgent", return_value=mock_agent),
    ):
        result = await triage_task(state)

    assert result["triage_passed"] is False
    assert result["current_node"] == "triage_gate"
    assert result["is_paused"] is True
    assert result["triage_missing_fields"] == expected_missing_list

    # Verify label change to TASK_TRIAGE_PENDING
    mock_jira.set_workflow_label.assert_called_once_with("TASK-123", ForgeLabel.TASK_TRIAGE_PENDING)

    # Verify detailed comment lists the missing fields
    assert mock_jira.add_comment.call_count == 2
    detailed_comment = mock_jira.add_comment.call_args_list[1].args[1]
    assert "starting with `!`" in detailed_comment
    for field in expected_missing_list:
        assert field in detailed_comment
