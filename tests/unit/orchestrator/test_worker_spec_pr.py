"""Tests for spec PR event handling in the worker."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage


def _make_message(event_type: str, payload: dict, ticket_key: str = "TEST-123") -> QueueMessage:
    return QueueMessage(
        message_id="msg-1",
        event_id="evt-1",
        source=EventSource.GITHUB,
        event_type=event_type,
        ticket_key=ticket_key,
        payload=payload,
    )


def _spec_gate_state(**overrides) -> dict:
    base = {
        "ticket_key": "TEST-123",
        "current_node": "spec_approval_gate",
        "is_paused": True,
        "spec_content": "# Spec",
        "spec_pr_number": 12,
        "spec_pr_repo": "org/proposals",
        "spec_pr_branch": "forge/spec/test-123",
        "spec_pr_url": "https://github.com/org/proposals/pull/12",
        "context": {},
        "last_error": None,
        "revision_requested": False,
        "feedback_comment": None,
        "is_question": False,
        "retry_count": 0,
        "is_blocked": False,
    }
    base.update(overrides)
    return base


@pytest.fixture
def worker():
    with patch("forge.orchestrator.worker.get_checkpointer"):
        w = OrchestratorWorker.__new__(OrchestratorWorker)
        w._post_terminal_error_comment = AsyncMock()
        return w


class TestHandleSpecPrMerge:
    @pytest.mark.asyncio
    async def test_pr_merge_uses_configured_custom_field_storage(self, worker):
        msg = _make_message(
            "pull_request:closed",
            {
                "repository": {"full_name": "org/proposals"},
                "pull_request": {"number": 12, "merged": True},
            },
        )
        state = _spec_gate_state()
        settings = MagicMock(
            jira_store_in_comments=False,
            jira_spec_custom_field="customfield_12345",
        )

        with (
            patch("forge.orchestrator.worker.get_settings", return_value=settings),
            patch("forge.orchestrator.worker.JiraClient") as MockJira,
        ):
            mock_jira = MagicMock()
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.update_custom_field = AsyncMock()
            mock_jira.add_structured_comment = AsyncMock()
            mock_jira.add_attachment = AsyncMock()
            mock_jira.delete_attachments_by_name = AsyncMock()
            mock_jira.close = AsyncMock()
            MockJira.return_value = mock_jira

            result = await worker._handle_resume_event(msg, state)

        assert result["is_paused"] is False
        mock_jira.set_workflow_label.assert_called_once()
        mock_jira.update_custom_field.assert_called_once_with(
            "TEST-123",
            "customfield_12345",
            "# Spec",
        )
        mock_jira.add_structured_comment.assert_not_called()
        mock_jira.add_attachment.assert_not_called()
