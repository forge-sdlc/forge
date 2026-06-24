"""Tests for run_task() trace field resolution.

Verifies that run_task() builds the trace_state correctly, calls
resolve_trace_fields(), and passes the resolved tags/metadata to
_run_agent().
"""

from unittest.mock import AsyncMock, patch

import pytest

from forge.integrations.agents.agent import ForgeAgent


@pytest.fixture
def agent() -> ForgeAgent:
    return ForgeAgent()


def _metrics_patches():
    """Common patches for the inline-imported metrics helpers."""
    return (
        patch("forge.api.routes.metrics.record_agent_invocation"),
        patch("forge.api.routes.metrics.observe_agent_duration"),
    )


class TestRunTaskTraceResolution:
    """run_task() resolves trace fields and forwards them to _run_agent()."""

    @pytest.mark.asyncio
    async def test_builds_trace_state_from_context_and_system_prompt(
        self, agent: ForgeAgent
    ) -> None:
        context = {"ticket_key": "PROJ-42", "current_node": "generate_prd"}

        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields"
            ) as mock_resolve,
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            mock_resolve.return_value = (["PROJ-42"], {"ticket_key": "PROJ-42"})

            await agent.run_task(task="generate-prd", prompt="test", context=context)

        # resolve_trace_fields should receive merged state with system_prompt_length and llm_model
        resolve_call_state = mock_resolve.call_args[0][0]
        assert resolve_call_state["ticket_key"] == "PROJ-42"
        assert resolve_call_state["current_node"] == "generate_prd"
        assert "system_prompt_length" in resolve_call_state
        assert isinstance(resolve_call_state["system_prompt_length"], int)
        assert resolve_call_state["llm_model"] == agent.settings.claude_model

    @pytest.mark.asyncio
    async def test_passes_resolved_tags_to_run_agent(self, agent: ForgeAgent) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=(["Bug", "PROJ"], {"ticket_key": "PROJ-42"}),
            ),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            await agent.run_task(
                task="test-task",
                prompt="test",
                context={"ticket_key": "PROJ-42"},
            )

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["tags"] == ["Bug", "PROJ"]
        assert call_kwargs["metadata"] == {"ticket_key": "PROJ-42"}

    @pytest.mark.asyncio
    async def test_uses_trace_context_ticket_key_for_session_when_context_omits_it(
        self, agent: ForgeAgent
    ) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=([], {}),
            ),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            await agent.run_task(
                task="test-task",
                prompt="test",
                context={"task_count": 2},
                trace_context={"ticket_key": "PROJ-42"},
            )

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["session_id"] == "PROJ-42"
        assert call_kwargs["ticket_key"] == "PROJ-42"
        assert "PROJ-42" not in call_kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_empty_tags_passed_as_none(self, agent: ForgeAgent) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=([], {}),
            ),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            await agent.run_task(task="test-task", prompt="test", context={})

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["tags"] is None
        assert call_kwargs["metadata"] is None

    @pytest.mark.asyncio
    async def test_none_context_produces_trace_state_with_prompt_and_model(
        self, agent: ForgeAgent
    ) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields"
            ) as mock_resolve,
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            mock_resolve.return_value = ([], {})
            await agent.run_task(task="test-task", prompt="test", context=None)

        resolve_call_state = mock_resolve.call_args[0][0]
        assert "system_prompt_length" in resolve_call_state
        assert "llm_model" in resolve_call_state
        # No other context keys should be present
        assert len(resolve_call_state) == 2

    @pytest.mark.asyncio
    async def test_trace_name_uses_task_prefix(self, agent: ForgeAgent) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=([], {}),
            ),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            await agent.run_task(task="generate-prd", prompt="test")

        assert mock_run.call_args.kwargs["trace_name"] == "task:generate-prd"

    @pytest.mark.asyncio
    async def test_session_id_from_ticket_key(self, agent: ForgeAgent) -> None:
        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=([], {}),
            ),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
        ):
            mock_run.return_value = "result"
            await agent.run_task(
                task="test", prompt="test", context={"ticket_key": "PROJ-42"}
            )

        assert mock_run.call_args.kwargs["session_id"] == "PROJ-42"
