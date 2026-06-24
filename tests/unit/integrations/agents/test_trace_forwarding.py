"""Tests for trace field forwarding in ForgeAgent.

Covers _forward_trace_fields() utility and the agent methods that
pass trace context through to run_task().
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from forge.integrations.agents.agent import ForgeAgent, _forward_trace_fields


class TestForwardTraceFields:
    """Unit tests for _forward_trace_fields()."""

    def test_extracts_known_trace_keys(self) -> None:
        context = {
            "ticket_key": "PROJ-42",
            "ticket_type": "Bug",
            "current_node": "analyze_bug",
            "current_repo": "acme/widgets",
            "current_pr_number": 99,
            "ci_status": "passed",
            "event_type": "issue_updated",
            "event_source": "jira",
            "retry_count": 3,
            "repo": "acme/widgets",
            "pr_number": 55,
        }
        result = _forward_trace_fields(context)
        assert result == context

    def test_filters_out_non_trace_keys(self) -> None:
        context = {
            "ticket_key": "PROJ-42",
            "project_key": "PROJ",
            "summary": "Fix the thing",
            "workspace_path": "/tmp/ws",
            "feature_summary": "A feature",
            "available_repos": ["acme/widgets"],
        }
        result = _forward_trace_fields(context)
        assert result == {"ticket_key": "PROJ-42"}

    def test_returns_empty_for_none(self) -> None:
        assert _forward_trace_fields(None) == {}

    def test_returns_empty_for_empty_dict(self) -> None:
        assert _forward_trace_fields({}) == {}

    def test_returns_empty_when_no_trace_keys_present(self) -> None:
        context = {"summary": "Something", "workspace_path": "/tmp"}
        assert _forward_trace_fields(context) == {}

    def test_preserves_original_values_unchanged(self) -> None:
        context = {
            "ticket_key": "PROJ-42",
            "retry_count": 0,
            "ci_status": "",
        }
        result = _forward_trace_fields(context)
        assert result["ticket_key"] == "PROJ-42"
        assert result["retry_count"] == 0
        assert result["ci_status"] == ""

    def test_does_not_mutate_input(self) -> None:
        context = {"ticket_key": "PROJ-42", "summary": "Test"}
        original = dict(context)
        _forward_trace_fields(context)
        assert context == original


class TestGeneratePrdTraceForwarding:
    """generate_prd() uses _forward_trace_fields() and adds project_key."""

    @pytest.mark.asyncio
    async def test_forwards_trace_fields_to_run_task(self) -> None:
        agent = ForgeAgent()
        context = {
            "ticket_key": "PROJ-42",
            "ticket_type": "Feature",
            "current_node": "generate_prd",
            "event_type": "issue_updated",
            "event_source": "jira",
            "retry_count": 1,
            "project_key": "PROJ",
            "summary": "Build auth",
        }

        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# PRD\n\nContent"
            await agent.generate_prd("Build auth system", context=context)

        # Prompt context contains only task-relevant fields
        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["ticket_key"] == "PROJ-42"
        assert call_ctx["project_key"] == "PROJ"
        assert "ticket_type" not in call_ctx
        assert "summary" not in call_ctx

        # Trace fields forwarded to trace_context, not the prompt
        call_trace = mock_run.call_args.kwargs["trace_context"]
        assert call_trace["ticket_type"] == "Feature"
        assert call_trace["current_node"] == "generate_prd"
        assert call_trace["event_type"] == "issue_updated"
        assert call_trace["event_source"] == "jira"
        assert call_trace["retry_count"] == 1

        rendered_prompt = mock_run.call_args.kwargs["prompt"]
        assert "ticket_type" not in rendered_prompt
        assert "current_node" not in rendered_prompt
        assert "event_type" not in rendered_prompt
        assert "event_source" not in rendered_prompt
        assert "retry_count" not in rendered_prompt

    @pytest.mark.asyncio
    async def test_handles_none_context(self) -> None:
        agent = ForgeAgent()
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# PRD\n\nContent"
            await agent.generate_prd("Build something", context=None)

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx == {"ticket_key": "", "project_key": ""}

    @pytest.mark.asyncio
    async def test_project_key_defaults_to_empty(self) -> None:
        agent = ForgeAgent()
        context: dict[str, Any] = {"ticket_key": "PROJ-42"}
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# PRD"
            await agent.generate_prd("Requirements", context=context)

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["project_key"] == ""


class TestGenerateSpecTraceForwarding:
    """generate_spec() uses _forward_trace_fields() and adds project_key."""

    @pytest.mark.asyncio
    async def test_forwards_trace_fields(self) -> None:
        agent = ForgeAgent()
        context = {
            "ticket_key": "PROJ-42",
            "ticket_type": "Feature",
            "current_node": "generate_spec",
            "event_type": "issue_updated",
            "project_key": "PROJ",
        }
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# Spec\n\nContent"
            await agent.generate_spec("PRD content", context=context)

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["ticket_key"] == "PROJ-42"
        assert call_ctx["project_key"] == "PROJ"
        assert "ticket_type" not in call_ctx

        call_trace = mock_run.call_args.kwargs["trace_context"]
        assert call_trace["ticket_type"] == "Feature"
        assert call_trace["current_node"] == "generate_spec"

        rendered_prompt = mock_run.call_args.kwargs["prompt"]
        assert "ticket_type" not in rendered_prompt
        assert "current_node" not in rendered_prompt
        assert "event_type" not in rendered_prompt


class TestGenerateEpicsTraceForwarding:
    """generate_epics() uses _forward_trace_fields() and adds extra context."""

    @pytest.mark.asyncio
    async def test_forwards_trace_fields_plus_extra(self) -> None:
        agent = ForgeAgent()
        context = {
            "ticket_key": "PROJ-42",
            "ticket_type": "Feature",
            "current_node": "decompose_epics",
            "event_type": "issue_updated",
            "event_source": "jira",
            "retry_count": 0,
            "project_key": "PROJ",
            "feature_summary": "Auth system",
            "available_repos": ["acme/backend", "acme/frontend"],
        }
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "---\nEPIC: Test\nREPO: acme/backend\nPLAN:\n1. Do it\n---"
            await agent.generate_epics("Spec content", context=context)

        # Prompt context contains only task-relevant fields
        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["ticket_key"] == "PROJ-42"
        assert call_ctx["project_key"] == "PROJ"
        assert call_ctx["feature_summary"] == "Auth system"
        assert call_ctx["available_repos"] == ["acme/backend", "acme/frontend"]
        assert "ticket_type" not in call_ctx
        assert "summary" not in call_ctx

        # Trace fields forwarded to trace_context only
        call_trace = mock_run.call_args.kwargs["trace_context"]
        assert call_trace["ticket_type"] == "Feature"
        assert call_trace["current_node"] == "decompose_epics"


class TestRegenerateWithFeedbackTraceForwarding:
    """regenerate_with_feedback() accepts context and uses _forward_trace_fields()."""

    @pytest.mark.asyncio
    async def test_forwards_trace_fields_from_context(self) -> None:
        agent = ForgeAgent()
        context = {
            "ticket_type": "Feature",
            "current_node": "regenerate_prd",
            "event_type": "issue_updated",
            "event_source": "jira",
            "retry_count": 2,
        }
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# Revised PRD"
            await agent.regenerate_with_feedback(
                original_content="# Old PRD",
                feedback="Add more detail",
                content_type="prd",
                ticket_key="PROJ-42",
                context=context,
            )

        # Prompt context contains only the minimal fields needed for the task
        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["is_revision"] is True
        assert call_ctx["ticket_key"] == "PROJ-42"
        assert "ticket_type" not in call_ctx

        # Trace fields forwarded to trace_context only
        call_trace = mock_run.call_args.kwargs["trace_context"]
        assert call_trace["ticket_type"] == "Feature"
        assert call_trace["current_node"] == "regenerate_prd"
        assert call_trace["event_type"] == "issue_updated"
        assert call_trace["event_source"] == "jira"
        assert call_trace["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_handles_none_context(self) -> None:
        agent = ForgeAgent()
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# Revised"
            await agent.regenerate_with_feedback(
                original_content="# Old",
                feedback="Fix it",
                content_type="spec",
                ticket_key="PROJ-42",
                context=None,
            )

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx == {"is_revision": True, "ticket_key": "PROJ-42"}

    @pytest.mark.asyncio
    async def test_ticket_key_defaults_to_empty(self) -> None:
        agent = ForgeAgent()
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "# Revised"
            await agent.regenerate_with_feedback(
                original_content="# Old",
                feedback="Fix it",
                content_type="prd",
                ticket_key=None,
                context=None,
            )

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["ticket_key"] == ""

    @pytest.mark.asyncio
    async def test_content_type_maps_to_correct_skill(self) -> None:
        agent = ForgeAgent()
        skill_map = {
            "prd": "generate-prd",
            "spec": "generate-spec",
            "epic": "decompose-epics",
            "task": "generate-tasks",
        }
        for content_type, expected_task in skill_map.items():
            with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = "# Result"
                await agent.regenerate_with_feedback(
                    original_content="# Old",
                    feedback="Fix",
                    content_type=content_type,
                )
            assert mock_run.call_args.kwargs["task"] == expected_task


class TestAnswerQuestionTraceForwarding:
    """answer_question() uses _forward_trace_fields() for context."""

    @pytest.mark.asyncio
    async def test_forwards_trace_fields(self) -> None:
        agent = ForgeAgent()
        context = {
            "ticket_key": "PROJ-42",
            "ticket_type": "Feature",
            "current_node": "answer_question",
            "event_type": "issue_updated",
            "event_source": "jira",
            "retry_count": 0,
            "artifact_type": "prd",
            "generation_context": {"raw_requirements": "Build API"},
        }
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "The answer"
            await agent.answer_question(
                question="Why REST?",
                artifact_content="# PRD\n\nWe use REST",
                context=context,
            )

        # Prompt context contains only task-relevant fields
        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["ticket_key"] == "PROJ-42"
        assert call_ctx["artifact_type"] == "prd"
        assert "ticket_type" not in call_ctx
        assert "generation_context" not in call_ctx

        # Trace fields forwarded to trace_context only
        call_trace = mock_run.call_args.kwargs["trace_context"]
        assert call_trace["ticket_type"] == "Feature"
        assert call_trace["current_node"] == "answer_question"
        assert call_trace["event_type"] == "issue_updated"
        assert call_trace["event_source"] == "jira"
        assert call_trace["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_artifact_type_defaults_to_document(self) -> None:
        agent = ForgeAgent()
        with patch.object(agent, "run_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = "Answer"
            await agent.answer_question(
                question="What?",
                artifact_content="Content",
                context={},
            )

        call_ctx = mock_run.call_args.kwargs["context"]
        assert call_ctx["artifact_type"] == "document"
