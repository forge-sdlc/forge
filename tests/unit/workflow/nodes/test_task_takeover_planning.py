"""Unit tests for task takeover planning nodes."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from forge.models.workflow import ForgeLabel
from forge.workflow.nodes.task_takeover_planning import (
    generate_plan,
    plan_approval_gate,
    route_plan_approval,
)
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


def make_task_state(**overrides: Any) -> TaskTakeoverState:
    """Create a TaskTakeoverState dict for planning tests."""
    state = create_initial_task_takeover_state("TASK-002")
    state_dict = cast(dict[str, Any], state)
    state_dict.update(overrides)
    return cast(TaskTakeoverState, state_dict)


@pytest.fixture
def base_task_state() -> TaskTakeoverState:
    return make_task_state()


def _make_mock_jira(summary="Implement user session logout", project_key="TASK"):
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = summary
    issue.description = "Task description"
    issue.project_key = project_key
    jira.get_issue = AsyncMock(return_value=issue)
    jira.get_comments = AsyncMock(return_value=[])
    jira.add_comment = AsyncMock()
    jira.add_labels = AsyncMock()
    jira.set_workflow_label = AsyncMock()
    jira.get_project_default_repo = AsyncMock(return_value="owner/project")
    jira.get_project_repos = AsyncMock(return_value=["owner/project"])
    jira.close = AsyncMock()
    return jira


def _make_mock_agent_success(
    plan_content="## Plan\n\nTask Takeover Plan details.\n\nrepo:owner/project",
) -> MagicMock:
    agent = MagicMock()
    agent.run_task = AsyncMock(return_value=plan_content)
    agent._strip_preamble.side_effect = lambda text: text
    agent.close = AsyncMock()
    return agent


def _make_mock_agent_failure() -> MagicMock:
    agent = MagicMock()
    agent.run_task = AsyncMock(side_effect=RuntimeError("Agent failed"))
    agent._strip_preamble.side_effect = lambda text: text
    agent.close = AsyncMock()
    return agent


class TestGeneratePlan:
    """Tests for the generate_plan node."""

    @pytest.mark.asyncio
    async def test_generate_plan_success(self, base_task_state: TaskTakeoverState) -> None:
        """Verify successful generation of task takeover plan."""
        mock_jira = _make_mock_jira()
        agent = _make_mock_agent_success(
            "## Plan\n\nTask Takeover Plan details.\n\nrepo:owner/project"
        )

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            result = await generate_plan(base_task_state)

        assert result["plan_content"] == "## Plan\n\nTask Takeover Plan details.\n\nrepo:owner/project"
        assert result["current_repo"] == "owner/project"
        assert result["repos_to_process"] == ["owner/project"]
        assert result["current_node"] == "task_plan_approval_gate"
        mock_jira.set_workflow_label.assert_called_once_with("TASK-002", ForgeLabel.PLAN_PENDING)
        mock_jira.add_labels.assert_called_once_with("TASK-002", ["repo:owner/project"])
        assert mock_jira.add_comment.call_count == 2  # Ack comment + Plan comment
        mock_jira.get_project_default_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_plan_with_truncation(self, base_task_state: TaskTakeoverState) -> None:
        """Verify plan comment is truncated if it exceeds maximum comment size."""
        mock_jira = _make_mock_jira()
        long_plan = "A" * 30_000 + "\n\nrepo:owner/project"
        agent = _make_mock_agent_success(long_plan)

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            await generate_plan(base_task_state)

        # Plan comment is the second comment
        plan_comment = mock_jira.add_comment.call_args_list[1].args[1]
        assert len(plan_comment) <= 25_500
        assert "truncated" in plan_comment.lower()

    @pytest.mark.asyncio
    async def test_generate_plan_failure_retries(self, base_task_state: TaskTakeoverState) -> None:
        """Verify agent failure increments retry_count and handles errors."""
        mock_jira = _make_mock_jira()
        agent = _make_mock_agent_failure()

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            result = await generate_plan(base_task_state)

        assert result["retry_count"] == 1
        assert result["last_error"] is not None
        assert result["current_node"] == "generate_plan"


class TestRegeneratePlanFlow:
    """Tests for the regeneration flow when a revision is requested."""

    @pytest.mark.asyncio
    async def test_regenerate_plan_with_feedback(self, base_task_state: TaskTakeoverState) -> None:
        """Verify regenerate plan with revision request and feedback details."""
        state = {
            **base_task_state,
            "revision_requested": True,
            "feedback_comment": "Please add more detailed logging.",
            "plan_content": "## Plan\n\nOld Plan content.",
        }

        mock_jira = _make_mock_jira()
        agent = _make_mock_agent_success(
            "## Plan\n\nNew Plan content with logging.\n\nrepo:owner/project"
        )

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            result = await generate_plan(state)

        assert result["plan_content"] == "## Plan\n\nNew Plan content with logging.\n\nrepo:owner/project"
        assert result["revision_requested"] is False
        assert result["feedback_comment"] is None
        assert result["current_node"] == "task_plan_approval_gate"

    @pytest.mark.asyncio
    async def test_generate_plan_does_not_fallback_to_first_project_repo(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Planning should not use repos[0] before the agent chooses a repo."""
        mock_jira = _make_mock_jira(project_key="AISOS")
        mock_jira.get_project_default_repo = AsyncMock(side_effect=Exception("not configured"))
        mock_jira.get_project_repos = AsyncMock(
            return_value=["openshift/installer", "forge-sdlc/forge"]
        )
        agent = _make_mock_agent_success(
            "## Plan\n\nUpdate README quick start instructions.\n\nrepo:forge-sdlc/forge"
        )

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            result = await generate_plan(base_task_state)

        assert result["current_repo"] == "forge-sdlc/forge"
        assert result["repos_to_process"] == ["forge-sdlc/forge"]
        mock_jira.get_project_default_repo.assert_not_called()
        mock_jira.add_labels.assert_called_once_with("TASK-002", ["repo:forge-sdlc/forge"])
        agent.run_task.assert_awaited_once()
        context = agent.run_task.call_args.kwargs["context"]
        assert context["current_repo"] == ""
        assert context["available_repos"] == ["openshift/installer", "forge-sdlc/forge"]

    @pytest.mark.asyncio
    async def test_generate_plan_retries_when_plan_has_no_valid_repo_tag(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """A plan must name a configured repo so setup_workspace can use the right target."""
        mock_jira = _make_mock_jira()
        agent = _make_mock_agent_success("## Plan\n\nTask Takeover Plan details.")

        with (
            patch("forge.workflow.nodes.task_takeover_planning.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.task_takeover_planning.ForgeAgent", return_value=agent),
        ):
            result = await generate_plan(base_task_state)

        assert result["retry_count"] == 1
        assert result["current_node"] == "generate_plan"
        assert "repo:<owner>/<repo>" in result["last_error"]


class TestPlanApprovalGate:
    """Tests for plan_approval_gate node."""

    def test_plan_approval_gate_pauses(self, base_task_state: TaskTakeoverState) -> None:
        """Verify plan_approval_gate pauses the state."""
        result = plan_approval_gate(base_task_state)
        assert result["is_paused"] is True
        assert result["current_node"] == "plan_approval_gate"


class TestRoutePlanApproval:
    """Tests for route_plan_approval function."""

    def test_route_plan_approval_paused(self, base_task_state: TaskTakeoverState) -> None:
        """Verify it returns END when state is paused."""
        state = {**base_task_state, "is_paused": True}
        assert route_plan_approval(state) == END

    def test_route_plan_approval_revision_requested(
        self, base_task_state: TaskTakeoverState
    ) -> None:
        """Verify it returns generate_plan when revision is requested and is_paused is False."""
        state = {**base_task_state, "is_paused": False, "revision_requested": True}
        assert route_plan_approval(state) == "generate_plan"

    def test_route_plan_approval_approved(self, base_task_state: TaskTakeoverState) -> None:
        """Verify it returns END when plan is approved (no other flags)."""
        state = {**base_task_state, "is_paused": False}
        assert route_plan_approval(state) == END
