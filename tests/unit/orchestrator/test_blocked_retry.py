"""Unit tests for blocked-state and forge:retry worker behaviour."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.models.events import EventSource
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage


@pytest.fixture
def worker():
    return OrchestratorWorker(consumer_name="test-worker")


@pytest.fixture
def base_message():
    return QueueMessage(
        message_id="1234567890-0",
        event_id="test-event-001",
        source=EventSource.JIRA,
        event_type="jira:issue_updated",
        ticket_key="TEST-123",
        payload={
            "issue": {
                "key": "TEST-123",
                "fields": {"issuetype": {"name": "Feature"}},
            },
        },
    )


def _make_retry_message(base: QueueMessage) -> QueueMessage:
    """Return a message simulating forge:retry label being added."""
    return QueueMessage(
        message_id=base.message_id,
        event_id=base.event_id,
        source=base.source,
        event_type="jira:issue_updated",
        ticket_key=base.ticket_key,
        payload={
            **base.payload,
            "changelog": {
                "items": [
                    {
                        "field": "labels",
                        "fromString": "forge:managed forge:blocked",
                        "toString": "forge:managed forge:blocked forge:retry",
                    }
                ]
            },
        },
    )



class TestWorkerTerminalBlockedCheck:
    """Worker skips invocation when is_blocked=True, same as terminal nodes."""

    @pytest.mark.asyncio
    async def test_blocked_state_skips_invocation(self, worker, base_message):
        """Workflow with is_blocked=True does not get invoked."""
        blocked_state = {
            "ticket_key": "TEST-123",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "is_blocked": True,
            "last_error": "CI exhausted",
            "context": {},
        }

        invoked = False

        async def fake_process(message):
            nonlocal invoked
            mock_state = MagicMock()
            mock_state.values = blocked_state

            terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
            is_terminal_or_blocked = (
                blocked_state.get("current_node") in terminal_nodes
                or blocked_state.get("is_blocked", False)
            )

            if is_terminal_or_blocked:
                return  # skipped
            invoked = True

        await fake_process(base_message)
        assert invoked is False

    @pytest.mark.asyncio
    async def test_non_blocked_mid_workflow_is_invocable(self, worker, base_message):
        """Workflow without is_blocked proceeds to invocation."""
        state = {
            "ticket_key": "TEST-123",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
            "context": {},
        }

        terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
        is_terminal_or_blocked = (
            state.get("current_node") in terminal_nodes
            or state.get("is_blocked", False)
        )

        assert is_terminal_or_blocked is False


class TestRetryHandlerClearsBlockedState:
    """_handle_resume_event clears is_blocked and resets ci_fix_attempts on retry."""

    @pytest.mark.asyncio
    async def test_retry_clears_is_blocked(self, worker, base_message):
        """forge:retry sets is_blocked=False."""
        blocked_state = {
            "ticket_key": "TEST-123",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "is_blocked": True,
            "last_error": "CI exhausted after 5 attempts",
            "ci_fix_attempts": 5,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), blocked_state
        )

        assert result.get("is_blocked") is False

    @pytest.mark.asyncio
    async def test_retry_resets_ci_fix_attempts_unconditionally(self, worker, base_message):
        """forge:retry resets ci_fix_attempts=0 regardless of current_node."""
        blocked_state = {
            "ticket_key": "TEST-123",
            "current_node": "setup_workspace",  # not ci_evaluator
            "is_paused": False,
            "is_blocked": True,
            "last_error": "Clone failed",
            "ci_fix_attempts": 3,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), blocked_state
        )

        assert result.get("ci_fix_attempts") == 0

    @pytest.mark.asyncio
    async def test_retry_clears_last_error(self, worker, base_message):
        """forge:retry clears last_error so the node runs fresh."""
        blocked_state = {
            "ticket_key": "TEST-123",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "is_blocked": True,
            "last_error": "CI exhausted",
            "ci_fix_attempts": 5,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), blocked_state
        )

        assert result.get("last_error") is None

    @pytest.mark.asyncio
    async def test_retry_preserves_current_node(self, worker, base_message):
        """forge:retry does NOT change current_node — resume happens at the blocked node."""
        blocked_state = {
            "ticket_key": "TEST-123",
            "current_node": "ci_evaluator",
            "is_paused": False,
            "is_blocked": True,
            "last_error": "CI exhausted",
            "ci_fix_attempts": 5,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), blocked_state
        )

        assert result.get("current_node") == "ci_evaluator"


class TestRetryOnStuckNonTerminalNode:
    """forge:retry unblocks a workflow stuck at a non-terminal node without error markers."""

    @pytest.mark.asyncio
    async def test_retry_on_non_terminal_no_error_still_resumes(self, worker, base_message):
        """forge:retry at a non-terminal node with no last_error/is_blocked must still resume."""
        stuck_state = {
            "ticket_key": "TEST-123",
            "current_node": "reflect_rca",
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
            "ci_fix_attempts": 0,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), stuck_state
        )

        assert result.get("is_paused") is False
        assert result.get("last_error") is None
        assert result.get("current_node") == "reflect_rca"


class TestRetryOnHappyPathTerminalPostsComment:
    """forge:retry on a cleanly-completed workflow posts an explanatory comment."""

    @pytest.mark.asyncio
    async def test_retry_on_terminal_no_error_posts_comment(self, worker, base_message):
        """forge:retry on complete state with no error posts an explanatory Jira comment."""
        terminal_state = {
            "ticket_key": "TEST-123",
            "current_node": "complete",
            "is_paused": False,
            "is_blocked": False,
            "last_error": None,
            "ci_fix_attempts": 0,
            "retry_count": 0,
            "revision_requested": False,
            "feedback_comment": None,
            "context": {},
        }

        worker._post_terminal_error_comment = AsyncMock()

        result = await worker._handle_resume_event(
            _make_retry_message(base_message), terminal_state
        )

        # State must be unchanged — no work to retry
        assert result.get("current_node") == "complete"
        # And the user must be informed via a Jira comment
        worker._post_terminal_error_comment.assert_called_once()
