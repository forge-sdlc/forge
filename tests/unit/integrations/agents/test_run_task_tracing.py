"""Tests for run_task() trace field resolution.

Verifies that run_task() builds the trace_state correctly, calls
resolve_trace_fields(), and passes the resolved tags/metadata to
_run_agent().
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.agents.agent import ForgeAgent


@pytest.fixture
def agent() -> ForgeAgent:
    agent = ForgeAgent()
    # Isolate tracing tests from model-policy values in the developer's .env.
    agent.settings = agent.settings.model_copy(deep=True)
    agent.settings.model_connections = {}
    agent.settings.model_policy = {}
    agent.settings.model_default = {}
    return agent


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
            patch("forge.integrations.agents.agent.resolve_trace_fields") as mock_resolve,
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
        assert resolve_call_state["llm_model"] == agent.settings.llm_model

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

    @pytest.mark.asyncio
    async def test_run_task_resolves_node_from_langgraph_config(self, agent: ForgeAgent) -> None:
        context = {"ticket_key": "PROJ-42"}
        config = {"metadata": {"langgraph_node": "generate_tasks"}}

        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "forge.integrations.agents.agent.resolve_trace_fields",
                return_value=([], {}),
            ) as mock_resolve,
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
            patch("langchain_core.runnables.config.ensure_config", return_value=config),
        ):
            mock_run.return_value = "result"
            await agent.run_task(task="generate-tasks", prompt="test", context=context)

        resolve_call_state = mock_resolve.call_args[0][0]
        assert resolve_call_state["current_node"] == "generate_tasks"

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
            patch("forge.integrations.agents.agent.resolve_trace_fields") as mock_resolve,
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
            await agent.run_task(task="test", prompt="test", context={"ticket_key": "PROJ-42"})

        assert mock_run.call_args.kwargs["session_id"] == "PROJ-42"

    @pytest.mark.asyncio
    async def test_project_policy_is_fetched_for_each_execution(self, agent: ForgeAgent) -> None:
        # Do not mutate the cached application Settings shared by later tests.
        agent.settings = agent.settings.model_copy(deep=True)
        agent.settings.model_connections = {
            "vertex": {
                "backend": "vertex-ai",
                "project": "test-project",
                "allowed_models": ["gemini-pro", "gemini-flash"],
                "capabilities": ["tools"],
            }
        }
        agent.settings.model_default = {"connection": "vertex", "model": "gemini-flash"}
        jira = MagicMock()
        project_policies = iter(
            [
                {"generate_prd": {"connection": "vertex", "model": "gemini-pro"}},
                None,
            ]
        )
        jira.get_project_property = AsyncMock(
            side_effect=lambda _project, prop: (
                next(project_policies) if prop == "forge.model_policy" else None
            )
        )
        jira.close = AsyncMock()

        with (
            patch.object(agent, "_run_agent", new_callable=AsyncMock) as mock_run,
            patch("forge.integrations.agents.agent.resolve_trace_fields", return_value=([], {})),
            patch("forge.integrations.agents.agent.load_prompt", return_value="prompt"),
            patch("forge.integrations.jira.client.JiraClient", return_value=jira),
        ):
            mock_run.return_value = "result"
            await agent.run_task(
                task="generate-prd", prompt="test", context={"ticket_key": "PROJ-42"}
            )
            await agent.run_task(
                task="generate-prd", prompt="test", context={"ticket_key": "PROJ-42"}
            )

        assert jira.get_project_property.await_count == 4
        assert mock_run.await_args_list[0].kwargs["model_target"].model == "gemini-pro"
        assert mock_run.await_args_list[1].kwargs["model_target"].model == "gemini-flash"
