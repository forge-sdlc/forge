"""Tests for trace context separation in PR creation helpers."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.pr_creation import _generate_pr_body_with_agent


@pytest.mark.asyncio
async def test_generate_pr_body_splits_prompt_context_from_trace_context() -> None:
    mock_jira = MagicMock()
    mock_jira.get_issue = AsyncMock()
    mock_issue = MagicMock()
    mock_issue.summary = "Task summary"
    mock_issue.description = "Task description"
    mock_jira.get_issue.return_value = mock_issue

    mock_git = MagicMock()
    mock_git._run_git.return_value = MagicMock(stdout="abc123 Test commit")

    mock_agent = MagicMock()
    mock_agent.run_task = AsyncMock(return_value="# PR Body\n\n" + "x" * 120)
    mock_agent._strip_preamble.side_effect = lambda text: text

    state = create_initial_feature_state(
        ticket_key="FEAT-123",
        ticket_type="Feature",
        current_node="create_pr",
        current_repo="owner/repo",
        current_pr_number=456,
        ci_status="pending",
        retry_count=2,
    )
    state["workspace_path"] = str(Path("/tmp/test-workspace"))
    state["context"] = {"source": "jira"}

    with patch("forge.workflow.nodes.pr_creation.ForgeAgent", return_value=mock_agent):
        result = await _generate_pr_body_with_agent(
            state,
            mock_git,
            mock_jira,
            ["TASK-1"],
        )

    assert result is not None
    call_kwargs = mock_agent.run_task.call_args.kwargs

    assert call_kwargs["context"] == {
        "ticket_key": "FEAT-123",
        "task_count": 1,
    }
    assert call_kwargs["trace_context"] == {
        "ticket_key": "FEAT-123",
        "ticket_type": "Feature",
        "current_node": "create_pr",
        "repo": "owner/repo",
        "pr_number": 456,
        "ci_status": "pending",
        "event_type": "",
        "event_source": "jira",
        "retry_count": 2,
    }
