"""Unit tests for triage_task node."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


def make_task_state(**overrides: Any) -> TaskTakeoverState:
    """Create a TaskTakeoverState dict for triage tests."""
    state = create_initial_task_takeover_state("TASK-001")
    state_dict = cast(dict[str, Any], state)
    state_dict.update(overrides)
    return cast(TaskTakeoverState, state_dict)


@pytest.fixture
def complete_ticket_state() -> TaskTakeoverState:
    """TaskTakeoverState with a well-specified ticket."""
    return make_task_state(
        current_node="start",
    )


@pytest.fixture
def resume_ticket_state() -> TaskTakeoverState:
    """TaskTakeoverState resuming from triage_gate."""
    return make_task_state(
        current_node="triage_gate",
        is_paused=True,
    )


@pytest.fixture
def mock_jira() -> MagicMock:
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value=MagicMock(
            summary="Login fails with special characters",
            description="Problem Statement: ... Proposed Solution/Approach: ... Acceptance Criteria: ...",
        )
    )
    jira.get_comments = AsyncMock(return_value=[])
    jira.add_comment = AsyncMock()
    jira.add_labels = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.get_project_repos = AsyncMock(return_value=["owner/project"])
    jira.get_project_default_repo = AsyncMock(return_value="owner/project")
    jira.close = AsyncMock()
    return jira


@pytest.fixture
def mock_agent_sufficient() -> MagicMock:
    """ForgeAgent that returns 'sufficient' for the triage prompt."""
    agent = MagicMock()
    agent.run_task = AsyncMock(return_value="sufficient")
    agent.close = AsyncMock()
    return agent


@pytest.fixture
def mock_agent_missing_fields() -> MagicMock:
    """ForgeAgent that returns a JSON list of missing fields."""
    agent = MagicMock()
    agent.run_task = AsyncMock(
        return_value='["Problem Statement", "Acceptance Criteria"]'
    )
    agent.close = AsyncMock()
    return agent


class TestTriageTaskSufficientTicket:
    """When the ticket has all required fields, triage passes."""

    @pytest.mark.asyncio
    async def test_sets_triage_passed_true(
        self,
        complete_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_sufficient: MagicMock,
    ) -> None:
        """triage_passed=True and transitions to generate_plan on success."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_task(complete_ticket_state)

        assert result["triage_passed"] is True
        assert result["current_node"] == "generate_plan"
        assert result["is_paused"] is False
        assert result["triage_missing_fields"] == []
        mock_jira.add_labels.assert_awaited_once_with("TASK-001", ["repo:owner/project"])

    @pytest.mark.asyncio
    async def test_acknowledgement_comment_posted_first(
        self,
        complete_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_sufficient: MagicMock,
    ) -> None:
        """Acknowledgement comment is posted before triage evaluation on first invocation."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        call_order: list[str] = []

        async def mock_comment(*_args: Any, **_kwargs: Any) -> MagicMock:
            call_order.append("comment")
            return MagicMock()

        async def mock_run_task(*_args: Any, **_kwargs: Any) -> str:
            call_order.append("agent")
            return "sufficient"

        mock_jira.add_comment.side_effect = mock_comment
        mock_agent_sufficient.run_task.side_effect = mock_run_task

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_task(complete_ticket_state)

        assert call_order[0] == "comment"
        assert mock_jira.add_comment.call_count == 2  # Ack comment + Success comment

    @pytest.mark.asyncio
    async def test_acknowledgement_comment_suppressed_on_resume(
        self,
        resume_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_sufficient: MagicMock,
    ) -> None:
        """Acknowledgement comment is bypassed when resuming from triage_gate."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            await triage_task(resume_ticket_state)

        # Only the pass comment should be posted on resume
        assert mock_jira.add_comment.call_count == 1
        comment_text = mock_jira.add_comment.call_args_list[0].args[1]
        assert "Thanks for the update" in comment_text

    @pytest.mark.asyncio
    async def test_resume_with_complete_ticket_consumes_revision_signal(
        self,
        resume_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_sufficient: MagicMock,
    ) -> None:
        """The ! comment used to resume triage must not make initial planning look like a revision."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        state = {
            **resume_ticket_state,
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": "!Proposed Solution/Approach: add a repo metadata flag.",
            "is_question": True,
        }

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_task(cast(TaskTakeoverState, state))

        assert result["triage_passed"] is True
        assert result["current_node"] == "generate_plan"
        assert result["is_paused"] is False
        assert result["is_question"] is False
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None

    @pytest.mark.asyncio
    async def test_sufficient_ticket_sets_inferred_repo(
        self,
        complete_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_sufficient: MagicMock,
    ) -> None:
        """Triage should resolve the repo before planning starts."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        issue = await mock_jira.get_issue("TASK-001")
        issue.summary = "Forge: allow repository metadata to open PRs as drafts first"
        mock_jira.get_project_repos = AsyncMock(
            return_value=["openshift/installer", "forge-sdlc/forge"]
        )
        mock_jira.get_project_default_repo = AsyncMock(return_value="openshift/installer")

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_sufficient,
            ),
        ):
            result = await triage_task(complete_ticket_state)

        assert result["triage_passed"] is True
        assert result["current_repo"] == "forge-sdlc/forge"
        mock_jira.add_labels.assert_awaited_once_with("TASK-001", ["repo:forge-sdlc/forge"])


class TestTriageTaskMissingFields:
    """When the ticket is missing required fields, triage pauses."""

    @pytest.mark.asyncio
    async def test_sets_triage_passed_false(
        self,
        complete_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_missing_fields: MagicMock,
    ) -> None:
        """triage_passed=False, is_paused=True, and transitions to triage_gate on failure."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            result = await triage_task(complete_ticket_state)

        assert result["triage_passed"] is False
        assert result["current_node"] == "triage_gate"
        assert result["is_paused"] is True
        assert "Problem Statement" in result["triage_missing_fields"]
        assert "Acceptance Criteria" in result["triage_missing_fields"]
        mock_jira.add_labels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_applies_triage_pending_label_and_posts_comment(
        self,
        complete_ticket_state: TaskTakeoverState,
        mock_jira: MagicMock,
        mock_agent_missing_fields: MagicMock,
    ) -> None:
        """Applies forge:task-triage-pending label and posts a detailed comment on failure."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
            patch(
                "forge.workflow.stations.triage.ForgeAgent",
                return_value=mock_agent_missing_fields,
            ),
        ):
            await triage_task(complete_ticket_state)

        mock_jira.set_workflow_label.assert_called_once_with(
            "TASK-001", ForgeLabel.TASK_TRIAGE_PENDING
        )
        assert mock_jira.add_comment.call_count == 2  # Ack comment + Missing fields comment
        missing_fields_comment = mock_jira.add_comment.call_args_list[1].args[1]
        assert "starting with `!`" in missing_fields_comment
        assert "Problem Statement" in missing_fields_comment
        assert "Acceptance Criteria" in missing_fields_comment


class TestTriageTaskErrorHandling:
    """Error handling and retry logic."""

    @pytest.mark.asyncio
    async def test_escalates_to_blocked_on_max_retries(self, mock_jira: MagicMock) -> None:
        """Transitions to escalate_blocked when max retries exceeded."""
        from forge.workflow.nodes.task_takeover_triage import triage_task

        state = make_task_state(retry_count=3)
        with (
            patch(
                "forge.workflow.nodes.task_takeover_triage.JiraClient", return_value=mock_jira
            ),
        ):
            result = await triage_task(state)

        assert result["current_node"] == "escalate_blocked"
        assert result["is_paused"] is False
