"""Tests for >option N detection in the orchestrator worker's _handle_resume_event."""

from unittest.mock import AsyncMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage


@pytest.fixture
def worker() -> OrchestratorWorker:
    return OrchestratorWorker(consumer_name="test-worker")


def _make_option_message(comment_body: str) -> QueueMessage:
    return QueueMessage(
        message_id="1234567890-0",
        event_id="test-event-001",
        source=EventSource.JIRA,
        event_type="comment_created",
        ticket_key="BUG-42",
        payload={
            "comment": {"body": comment_body},
            "changelog": {"items": []},
        },
    )


def _make_rca_gate_state(**overrides) -> dict:
    base = {
        "ticket_key": "BUG-42",
        "ticket_type": "Bug",
        "current_node": "rca_option_gate",
        "is_paused": True,
        "revision_requested": False,
        "feedback_comment": None,
        "is_question": False,
        "selected_fix_option": None,
        "selected_fix_approach": None,
        "rca_options": [
            {"title": "Option A", "description": "Fix the null check", "tradeoffs": "Low risk"},
            {"title": "Option B", "description": "Refactor auth flow", "tradeoffs": "Higher risk"},
        ],
        "context": {},
    }
    return {**base, **overrides}


class TestOptionNDetection:
    @pytest.mark.asyncio
    async def test_option_comment_sets_selected_fix_option(self, worker):
        """>option 2 comment → selected_fix_option=2, selected_fix_approach=rca_options[1]."""
        message = _make_option_message(">option 2")
        state = _make_rca_gate_state()

        result = await worker._handle_resume_event(message, state)

        assert result["selected_fix_option"] == 2
        assert result["selected_fix_approach"] == state["rca_options"][1]
        assert result["is_paused"] is False

    @pytest.mark.asyncio
    async def test_option_detection_case_insensitive(self, worker):
        """>Option 2 (capital O) matches correctly."""
        message = _make_option_message(">Option 2")
        state = _make_rca_gate_state()

        result = await worker._handle_resume_event(message, state)

        assert result["selected_fix_option"] == 2

    @pytest.mark.asyncio
    async def test_option_detection_in_prose(self, worker):
        """'let's go with >option 1 based on discussion' → selected_fix_option=1."""
        message = _make_option_message("let's go with >option 1 based on discussion")
        state = _make_rca_gate_state()

        result = await worker._handle_resume_event(message, state)

        assert result["selected_fix_option"] == 1
        assert result["selected_fix_approach"] == state["rca_options"][0]

    @pytest.mark.asyncio
    async def test_first_match_wins(self, worker):
        """Multiple >option lines in comment → first match is used."""
        message = _make_option_message(">option 1\n>option 2")
        state = _make_rca_gate_state()

        result = await worker._handle_resume_event(message, state)

        assert result["selected_fix_option"] == 1

    @pytest.mark.asyncio
    async def test_out_of_range_option_posts_clarifying_comment(self, worker):
        """>option 5 when only 2 options → clarifying comment posted."""
        message = _make_option_message(">option 5")
        state = _make_rca_gate_state()
        mock_jira = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira):
            await worker._handle_resume_event(message, state)

        mock_jira.add_comment.assert_called_once()
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "option" in comment_text.lower() and ("1" in comment_text and "2" in comment_text)

    @pytest.mark.asyncio
    async def test_out_of_range_option_does_not_update_state(self, worker):
        """>option 5 when only 2 options → selected_fix_option remains None."""
        message = _make_option_message(">option 5")
        state = _make_rca_gate_state()
        mock_jira = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        with patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira):
            result = await worker._handle_resume_event(message, state)

        assert result["selected_fix_option"] is None
        assert result is state  # Should return current_state unchanged

    @pytest.mark.asyncio
    async def test_no_option_pattern_falls_through_to_revision(self, worker):
        """Comment with ! prefix and no >option → revision_requested=True."""
        message = _make_option_message("!I think the analysis missed the real root cause.")
        state = _make_rca_gate_state()

        mock_jira = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.orchestrator.worker.JiraClient", return_value=mock_jira),
            patch("forge.orchestrator.worker.post_status_comment", new_callable=AsyncMock),
        ):
            result = await worker._handle_resume_event(message, state)

        assert result["revision_requested"] is True
        assert result["selected_fix_option"] is None

    @pytest.mark.asyncio
    async def test_option_detection_only_at_rca_option_gate(self, worker):
        """>option N comment at a different node does not set selected_fix_option."""
        message = _make_option_message(">option 1")
        state = _make_rca_gate_state(current_node="prd_approval_gate")

        result = await worker._handle_resume_event(message, state)

        assert result.get("selected_fix_option") is None
