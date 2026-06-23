"""Tests that workflow nodes pass trace-related context fields to the agent.

Each workflow node was updated to include trace fields (ticket_type, current_node,
event_type, event_source, retry_count, etc.) in the context dict passed to the
agent. These tests verify the context dicts contain the expected trace keys.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType

TRACE_CONTEXT_KEYS = {
    "ticket_key",
    "ticket_type",
    "current_node",
    "event_type",
    "event_source",
    "retry_count",
}


def _make_feature_state(**overrides: Any) -> dict[str, Any]:
    """Build a minimal feature workflow state."""
    from forge.workflow.feature.state import create_initial_feature_state

    state = create_initial_feature_state(
        ticket_key="TEST-123",
        ticket_type=TicketType.FEATURE,
    )
    state["current_node"] = "generate_prd"
    state["event_type"] = "issue_updated"
    state["context"] = {"source": "jira"}
    state["retry_count"] = 0
    state.update(overrides)
    return state


def _make_bug_state(**overrides: Any) -> dict[str, Any]:
    """Build a minimal bug workflow state."""
    state: dict[str, Any] = {
        "thread_id": "test-thread",
        "ticket_key": "BUG-456",
        "ticket_type": "Bug",
        "current_node": "analyze_bug",
        "event_type": "issue_updated",
        "context": {"source": "jira"},
        "ci_status": "",
        "retry_count": 0,
        "is_paused": False,
        "error_message": None,
    }
    state.update(overrides)
    return state


class TestPrdGenerationTraceContext:
    """generate_prd node includes trace fields in agent context."""

    @pytest.mark.asyncio
    async def test_generate_prd_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.prd_generation import generate_prd

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(
                summary="Test Feature",
                description="Build something",
                project_key="TEST",
            )
        )
        mock_jira.add_comment = AsyncMock()
        mock_jira.add_structured_comment = AsyncMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_generate_prd(raw_req, context=None):
            if context:
                captured_context.update(context)
            return "# PRD\n\nContent"

        mock_agent.generate_prd = capture_generate_prd

        state = _make_feature_state(current_node="generate_prd")

        with (
            patch(
                "forge.workflow.nodes.prd_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.prd_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await generate_prd(state)

        for key in TRACE_CONTEXT_KEYS:
            assert key in captured_context, f"Missing trace key '{key}' in PRD context"

    @pytest.mark.asyncio
    async def test_regenerate_prd_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.prd_generation import regenerate_prd_with_feedback

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.add_comment = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_regen(**kwargs):
            if kwargs.get("context"):
                captured_context.update(kwargs["context"])
            return "# Revised PRD"

        mock_agent.regenerate_with_feedback = capture_regen

        state = _make_feature_state(
            current_node="regenerate_prd",
            prd_content="# Old PRD",
            feedback_comment="Add more detail",
            revision_requested=True,
        )

        with (
            patch(
                "forge.workflow.nodes.prd_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.prd_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await regenerate_prd_with_feedback(state)

        for key in {"ticket_type", "current_node", "event_type", "event_source", "retry_count"}:
            assert key in captured_context, f"Missing trace key '{key}' in PRD regen context"


class TestSpecGenerationTraceContext:
    """generate_spec node includes trace fields in agent context."""

    @pytest.mark.asyncio
    async def test_generate_spec_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.spec_generation import generate_spec

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.add_structured_comment = AsyncMock()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(
                summary="Test Feature",
                description="Test description",
                project_key="TEST",
            )
        )
        mock_jira.get_prd_proposals_repo = AsyncMock(return_value=None)
        mock_jira.get_proposals_path = AsyncMock(return_value=None)

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_generate_spec(prd, context=None):
            if context:
                captured_context.update(context)
            return "# Spec\n\nContent"

        mock_agent.generate_spec = capture_generate_spec

        state = _make_feature_state(
            current_node="generate_spec",
            prd_content="# PRD content",
            qa_history=[],
        )

        with (
            patch(
                "forge.workflow.nodes.spec_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.spec_generation.ForgeAgent",
                return_value=mock_agent,
            ),
            patch("forge.workflow.nodes.spec_generation.post_qa_summary_if_needed"),
        ):
            await generate_spec(state)

        for key in TRACE_CONTEXT_KEYS:
            assert key in captured_context, f"Missing trace key '{key}' in spec context"

    @pytest.mark.asyncio
    async def test_regenerate_spec_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.spec_generation import regenerate_spec_with_feedback

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.update_description = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.add_structured_comment = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_regen(**kwargs):
            if kwargs.get("context"):
                captured_context.update(kwargs["context"])
            return "# Revised Spec"

        mock_agent.regenerate_with_feedback = capture_regen

        state = _make_feature_state(
            current_node="regenerate_spec",
            spec_content="# Old Spec",
            prd_content="# PRD",
            feedback_comment="Change approach",
            revision_requested=True,
        )

        with (
            patch(
                "forge.workflow.nodes.spec_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.spec_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await regenerate_spec_with_feedback(state)

        for key in {"ticket_type", "current_node", "event_type", "event_source", "retry_count"}:
            assert key in captured_context, f"Missing trace key '{key}' in spec regen context"


class TestQaHandlerTraceContext:
    """answer_question node includes trace fields in agent context."""

    @pytest.mark.asyncio
    async def test_answer_question_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.qa_handler import answer_question

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.add_comment = AsyncMock(return_value=MagicMock(id="c-1"))

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_answer(question, artifact_content, context):
            captured_context.update(context)
            return "The answer"

        mock_agent.answer_question = capture_answer

        state = _make_feature_state(
            current_node="prd_approval_gate",
            feedback_comment="?Why this approach?",
            is_question=True,
            prd_content="# PRD",
        )

        with (
            patch(
                "forge.workflow.nodes.qa_handler.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.qa_handler.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await answer_question(state)

        for key in TRACE_CONTEXT_KEYS:
            assert key in captured_context, f"Missing trace key '{key}' in QA context"


class TestEpicDecompositionTraceContext:
    """decompose_epics node includes trace fields in agent context."""

    @pytest.mark.asyncio
    async def test_decompose_epics_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.epic_decomposition import decompose_epics

        mock_jira = AsyncMock()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(
                project_key="TEST",
                summary="Test Feature",
            )
        )
        mock_jira.get_labels = AsyncMock(return_value=[])
        mock_jira.get_project_repos = AsyncMock(return_value=["acme/backend"])
        mock_jira.create_epic = AsyncMock(return_value="TEST-200")
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.add_comment = AsyncMock()

        mock_agent = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_epics(spec, context=None):
            if context:
                captured_context.update(context)
            return [{"summary": "Epic 1", "plan": "Do it", "repo": "acme/backend"}]

        mock_agent.generate_epics = capture_epics

        state = _make_feature_state(
            current_node="decompose_epics",
            spec_content="# Spec content",
            generation_context={},
            qa_history=[],
        )

        with (
            patch(
                "forge.workflow.nodes.epic_decomposition.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.epic_decomposition.ForgeAgent",
                return_value=mock_agent,
            ),
            patch("forge.workflow.nodes.epic_decomposition.post_qa_summary_if_needed"),
        ):
            await decompose_epics(state)

        for key in TRACE_CONTEXT_KEYS:
            assert key in captured_context, f"Missing trace key '{key}' in epic context"

    @pytest.mark.asyncio
    async def test_update_single_epic_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.epic_decomposition import update_single_epic

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(description="Original epic")
        )
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_regen(**kwargs):
            if kwargs.get("context"):
                captured_context.update(kwargs["context"])
            return "# Revised Epic"

        mock_agent.regenerate_with_feedback = capture_regen

        state = _make_feature_state(
            current_node="update_single_epic",
            epic_keys=["TEST-200"],
            current_epic_key="TEST-200",
            feedback_comment="Change scope",
            revision_requested=True,
        )

        with (
            patch(
                "forge.workflow.nodes.epic_decomposition.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.epic_decomposition.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await update_single_epic(state)

        for key in {"ticket_type", "current_node", "event_type", "event_source", "retry_count"}:
            assert key in captured_context, f"Missing trace key '{key}' in epic update context"


class TestTaskGenerationTraceContext:
    """update_single_task node includes trace fields in agent context."""

    @pytest.mark.asyncio
    async def test_update_single_task_passes_trace_fields(self) -> None:
        from forge.workflow.nodes.task_generation import update_single_task

        mock_jira = MagicMock()
        mock_jira.close = AsyncMock()
        mock_jira.get_issue = AsyncMock(
            return_value=MagicMock(description="Original task")
        )
        mock_jira.update_description = AsyncMock()
        mock_jira.add_comment = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.close = AsyncMock()
        captured_context: dict[str, Any] = {}

        async def capture_regen(**kwargs):
            if kwargs.get("context"):
                captured_context.update(kwargs["context"])
            return "# Revised Task"

        mock_agent.regenerate_with_feedback = capture_regen

        state = _make_feature_state(
            current_node="update_single_task",
            current_task_key="TEST-300",
            feedback_comment="Make it smaller",
            revision_requested=True,
        )

        with (
            patch(
                "forge.workflow.nodes.task_generation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.task_generation.ForgeAgent",
                return_value=mock_agent,
            ),
        ):
            await update_single_task(state)

        for key in {"ticket_type", "current_node", "event_type", "event_source", "retry_count"}:
            assert key in captured_context, f"Missing trace key '{key}' in task update context"
