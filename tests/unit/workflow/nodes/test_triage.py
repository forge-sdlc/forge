"""Unit tests for triage_check and triage_gate nodes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.bug.state import create_initial_bug_state


def make_bug_state(**overrides):
    """Create a BugState dict for triage tests."""
    state = create_initial_bug_state("BUG-001")
    state.update(overrides)
    return state


@pytest.fixture
def complete_ticket_state():
    """BugState with a well-specified ticket in context."""
    return make_bug_state(
        current_node="triage_check",
        context={
            "summary": "Login fails with special characters in password",
            "description": (
                "Steps to reproduce: 1. Enter p@ssw0rd! 2. Click login\n"
                "Expected: login succeeds. Actual: 401 Unauthorized\n"
                "Environment: Ubuntu 22.04, Python 3.11, production\n"
                "Affected versions: v2.3.0\n"
                "Error: AuthenticationError: invalid credentials at auth/validators.py:23\n"
                "Component: auth-service"
            ),
            "comments": [],
        },
    )


@pytest.fixture
def incomplete_ticket_state():
    """BugState with a ticket missing steps-to-reproduce and environment."""
    return make_bug_state(
        current_node="triage_check",
        context={
            "summary": "Login is broken",
            "description": "Login doesn't work for some users.",
            "comments": [],
        },
    )


@pytest.fixture
def mock_jira():
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value=MagicMock(
            summary="Login fails with special characters",
            description="Steps to reproduce: ...",
        )
    )
    jira.get_comments = AsyncMock(return_value=[])
    jira.add_comment = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.close = AsyncMock()
    return jira


@pytest.fixture
def mock_agent_sufficient():
    """ForgeAgent that returns 'sufficient' for the triage prompt."""
    agent = MagicMock()
    agent.run_task = AsyncMock(return_value="sufficient")
    agent.close = AsyncMock()
    return agent


@pytest.fixture
def mock_agent_missing_fields():
    """ForgeAgent that returns a JSON list of missing fields."""
    agent = MagicMock()
    agent.run_task = AsyncMock(
        return_value='["steps_to_reproduce", "environment"]'
    )
    agent.close = AsyncMock()
    return agent


class TestTriageCheckSufficientTicket:
    """When the ticket has all required fields, triage passes immediately."""

    @pytest.mark.asyncio
    async def test_sets_triage_passed_true(
        self, complete_ticket_state, mock_jira, mock_agent_sufficient
    ):
        """triage_passed=True when agent returns 'sufficient'."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_check(complete_ticket_state)
        assert result["triage_passed"] is True

    @pytest.mark.asyncio
    async def test_missing_fields_empty(
        self, complete_ticket_state, mock_jira, mock_agent_sufficient
    ):
        """triage_missing_fields=[] on sufficient ticket."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_check(complete_ticket_state)
        assert result["triage_missing_fields"] == []

    @pytest.mark.asyncio
    async def test_no_triage_pending_label_set(
        self, complete_ticket_state, mock_jira, mock_agent_sufficient
    ):
        """forge:triage-pending label is NOT set when ticket is sufficient."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_check(complete_ticket_state)
        for call in mock_jira.set_workflow_label.call_args_list:
            assert ForgeLabel.TRIAGE_PENDING not in call.args

    @pytest.mark.asyncio
    async def test_acknowledgement_comment_posted_first(
        self, complete_ticket_state, mock_jira, mock_agent_sufficient
    ):
        """Acknowledgement comment is posted before triage evaluation on first invocation."""
        from forge.workflow.nodes.triage import triage_check

        call_order = []
        mock_jira.add_comment = AsyncMock(
            side_effect=lambda *_a, **_k: call_order.append("comment")
        )
        mock_agent_sufficient.run_task = AsyncMock(
            side_effect=lambda *_a, **_k: call_order.append("agent") or "sufficient"
        )
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_check(complete_ticket_state)
        assert call_order[0] == "comment", "Acknowledgement comment must be first"

    @pytest.mark.asyncio
    async def test_acknowledgement_comment_suppressed_on_resume(
        self, mock_jira, mock_agent_sufficient
    ):
        """Ack comment is NOT re-posted when resuming from triage_gate."""
        from forge.workflow.nodes.triage import triage_check

        state = make_bug_state(
            current_node="triage_gate",  # indicates resume
            is_paused=True,
            triage_passed=False,
            triage_missing_fields=["steps_to_reproduce"],
        )
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_check(state)
        # On resume, the initial ack is suppressed but the pass comment is still posted
        assert mock_jira.add_comment.call_count == 1
        resume_comment = mock_jira.add_comment.call_args_list[0].args[1]
        assert "Thanks for the update" in resume_comment or "analysis" in resume_comment.lower()

    @pytest.mark.asyncio
    async def test_acknowledgement_comment_content(
        self, complete_ticket_state, mock_jira, mock_agent_sufficient
    ):
        """Acknowledgement comment mentions RCA and fix options."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_check(complete_ticket_state)
        # First comment: received/checking ack; second comment: triage passed, starting analysis
        assert mock_jira.add_comment.call_count == 2
        ack_comment = mock_jira.add_comment.call_args_list[0].args[1]
        pass_comment = mock_jira.add_comment.call_args_list[1].args[1]
        assert "completeness" in ack_comment.lower() or "received" in ack_comment.lower()
        assert "analysis" in pass_comment.lower()


class TestTriageCheckMissingFields:
    """When the ticket is missing required fields, triage pauses for reporter."""

    @pytest.mark.asyncio
    async def test_sets_triage_passed_false(
        self, incomplete_ticket_state, mock_jira, mock_agent_missing_fields
    ):
        """triage_passed=False when agent returns missing fields."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            result = await triage_check(incomplete_ticket_state)
        assert result["triage_passed"] is False

    @pytest.mark.asyncio
    async def test_missing_fields_populated(
        self, incomplete_ticket_state, mock_jira, mock_agent_missing_fields
    ):
        """triage_missing_fields contains the missing field names."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            result = await triage_check(incomplete_ticket_state)
        assert "steps_to_reproduce" in result["triage_missing_fields"]
        assert "environment" in result["triage_missing_fields"]

    @pytest.mark.asyncio
    async def test_targeted_comment_posted(
        self, incomplete_ticket_state, mock_jira, mock_agent_missing_fields
    ):
        """A targeted comment naming only the missing fields is posted."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            await triage_check(incomplete_ticket_state)
        # At least 2 comments: acknowledgement + missing fields
        assert mock_jira.add_comment.call_count >= 2
        last_comment = mock_jira.add_comment.call_args_list[-1].args[1]
        assert (
            "steps_to_reproduce" in last_comment
            or "steps to reproduce" in last_comment.lower()
        )

    @pytest.mark.asyncio
    async def test_triage_pending_label_set(
        self, incomplete_ticket_state, mock_jira, mock_agent_missing_fields
    ):
        """forge:triage-pending label is applied when fields are missing."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            await triage_check(incomplete_ticket_state)
        mock_jira.set_workflow_label.assert_called_with(
            incomplete_ticket_state["ticket_key"], ForgeLabel.TRIAGE_PENDING
        )

    @pytest.mark.asyncio
    async def test_current_node_set_to_triage_gate(
        self, incomplete_ticket_state, mock_jira, mock_agent_missing_fields
    ):
        """current_node='triage_gate' after missing-fields result."""
        from forge.workflow.nodes.triage import triage_check

        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            result = await triage_check(incomplete_ticket_state)
        assert result["current_node"] == "triage_gate"


class TestTriageCheckResume:
    """triage_check re-evaluates on resume after reporter updates ticket."""

    @pytest.mark.asyncio
    async def test_resume_with_complete_ticket_passes(
        self, mock_jira, mock_agent_sufficient
    ):
        """On resume, if ticket now has all fields, triage_passed=True."""
        from forge.workflow.nodes.triage import triage_check

        state = make_bug_state(
            current_node="triage_gate",
            is_paused=True,
            triage_passed=False,
            triage_missing_fields=["steps_to_reproduce"],
        )
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_check(state)
        assert result["triage_passed"] is True

    @pytest.mark.asyncio
    async def test_resume_still_missing_reposts_comment(
        self, mock_jira, mock_agent_missing_fields
    ):
        """On resume, still-missing fields cause a fresh targeted comment."""
        from forge.workflow.nodes.triage import triage_check

        state = make_bug_state(
            current_node="triage_gate",
            is_paused=True,
            triage_passed=False,
            triage_missing_fields=["steps_to_reproduce"],
        )
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            result = await triage_check(state)
        assert result["triage_passed"] is False
        # On resume, ack is suppressed — only the missing-fields comment is posted
        assert mock_jira.add_comment.call_count >= 1


class TestTriageCheckErrorHandling:
    """triage_check retries on failure and escalates after 3 failures."""

    @pytest.mark.asyncio
    async def test_failure_increments_retry_count(
        self, incomplete_ticket_state, mock_jira
    ):
        """Node failure increments retry_count."""
        from forge.workflow.nodes.triage import triage_check

        mock_agent = MagicMock()
        mock_agent.run_task = AsyncMock(side_effect=Exception("API error"))
        mock_agent.close = AsyncMock()
        incomplete_ticket_state["retry_count"] = 1
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent", return_value=mock_agent
            ),
        ):
            result = await triage_check(incomplete_ticket_state)
        assert result["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_after_3_failures_escalates_blocked(
        self, incomplete_ticket_state, mock_jira
    ):
        """After 3 consecutive failures (retry_count already at max), routes to escalate_blocked."""
        from forge.workflow.nodes.triage import triage_check

        mock_agent = MagicMock()
        mock_agent.run_task = AsyncMock(side_effect=Exception("API error"))
        mock_agent.close = AsyncMock()
        incomplete_ticket_state["retry_count"] = 3
        with (
            patch(
                "forge.workflow.nodes.triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.nodes.triage.ForgeAgent", return_value=mock_agent
            ),
        ):
            result = await triage_check(incomplete_ticket_state)
        assert result["current_node"] == "escalate_blocked"


class TestTriageGateNode:
    """triage_gate pauses the workflow and routes correctly on resume."""

    def test_set_paused_called(self):
        """triage_gate sets is_paused=True and current_node='triage_gate'."""
        from forge.workflow.nodes.triage import triage_gate

        state = make_bug_state()
        result = triage_gate(state)
        assert result["is_paused"] is True
        assert result["current_node"] == "triage_gate"

    def test_routing_returns_end_when_paused(self):
        """route_triage_gate returns END when is_paused=True."""
        from langgraph.graph import END

        from forge.workflow.nodes.triage import route_triage_gate

        state = make_bug_state(current_node="triage_gate", is_paused=True)
        assert route_triage_gate(state) == END

    def test_routing_returns_triage_check_on_resume(self):
        """route_triage_gate returns 'triage_check' when is_paused=False."""
        from forge.workflow.nodes.triage import route_triage_gate

        state = make_bug_state(current_node="triage_gate", is_paused=False)
        assert route_triage_gate(state) == "triage_check"
