from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, ConfigDict

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.structured_outputs import ArtifactDocument, TaskGeneration


def test_artifact_document_requires_repository_names() -> None:
    with pytest.raises(ValueError, match="owner/repository"):
        ArtifactDocument(content="Document", repositories=["not-a-repo"])


def test_task_generation_requires_owner_repository_repo_field() -> None:
    with pytest.raises(ValueError, match="owner/repository"):
        TaskGeneration.model_validate(
            {
                "tasks": [
                    {
                        "summary": "Implement it",
                        "description": "Implement the change.",
                        "repo": "unknown",
                    }
                ]
            }
        )


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    reason: str


@tool
async def repeat_probe() -> str:
    """Return a harmless value for a synthetic repeating tool loop."""
    return "again"


class RepeatingToolModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "repeating-tool-test"

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> Any:
        return self

    def _generate(self, _messages: Any, **_kwargs: Any) -> ChatResult:
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "repeat_probe", "args": {}, "id": f"call-{self.calls}"}
                        ],
                    )
                )
            ]
        )

    async def _agenerate(self, messages: Any, **kwargs: Any) -> ChatResult:
        return self._generate(messages, **kwargs)


@pytest.mark.asyncio
async def test_tool_loop_returns_validated_structured_response() -> None:
    forge = ForgeAgent()
    deep_agent = AsyncMock()
    deep_agent.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": True, "reason": "valid"},
    }

    with patch.object(forge, "_create_agent_async", return_value=deep_agent) as create:
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=True, reason="valid")
    assert isinstance(create.call_args.kwargs["response_format"], ProviderStrategy)
    deep_agent.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_native_response_retries_with_validated_tool_strategy() -> None:
    forge = ForgeAgent()
    native = AsyncMock()
    native.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": "not-a-boolean", "unexpected": True},
    }
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": False, "reason": "rejected"},
    }

    with patch.object(forge, "_create_agent_async", side_effect=[native, fallback]) as create:
        result = await forge._run_agent("prompt", "system", response_schema=Decision)

    assert result == Decision(accepted=False, reason="rejected")
    assert isinstance(create.call_args_list[0].kwargs["response_format"], ProviderStrategy)
    assert isinstance(create.call_args_list[1].kwargs["response_format"], ToolStrategy)


@pytest.mark.asyncio
async def test_invalid_fallback_response_raises_actionable_validation_error() -> None:
    forge = ForgeAgent()
    native = AsyncMock()
    native.ainvoke.side_effect = ValueError("provider schema unsupported")
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {"messages": [], "structured_response": {"accepted": True}}

    with (
        patch.object(forge, "_create_agent_async", side_effect=[native, fallback]),
        pytest.raises(ValueError, match="reason"),
    ):
        await forge._run_agent("prompt", "system", response_schema=Decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_schema", [None, Decision])
async def test_graph_recursion_exhaustion_never_retries_or_falls_back(
    response_schema: type[Decision] | None,
) -> None:
    forge = ForgeAgent()
    graph = AsyncMock()
    graph.ainvoke.side_effect = GraphRecursionError("recursion limit reached")

    with (
        patch.object(forge, "_create_agent_async", return_value=graph) as create,
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(GraphRecursionError),
    ):
        await forge._run_agent(
            "prompt",
            "system",
            response_schema=response_schema,
            recursion_limit=4,
            ticket_key="PROJ-42",
            trace_name="task:generate-tasks",
        )

    create.assert_awaited_once()
    graph.ainvoke.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_limit_overrides_tracing_config_without_losing_callbacks() -> None:
    forge = ForgeAgent()
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": []}
    with (
        patch.object(forge, "_create_agent_async", return_value=graph),
        patch(
            "forge.integrations.agents.agent.get_langfuse_config",
            return_value={
                "recursion_limit": 999,
                "callbacks": ["trace-callback"],
                "configurable": {"thread_id": "tracing-overwrite"},
            },
        ),
    ):
        await forge._run_agent("prompt", "system", recursion_limit=4)

    config = graph.ainvoke.await_args.kwargs["config"]
    assert config["recursion_limit"] == 4
    assert config["callbacks"] == ["trace-callback"]
    assert config["configurable"]["thread_id"] != "tracing-overwrite"
    assert config["configurable"]["thread_id"]


@pytest.mark.asyncio
async def test_fallback_and_transient_retry_keep_same_limit() -> None:
    forge = ForgeAgent()
    native = AsyncMock()
    native.ainvoke.side_effect = ValueError("native structured output unavailable")
    fallback = AsyncMock()
    fallback.ainvoke.return_value = {
        "messages": [],
        "structured_response": {"accepted": True, "reason": "valid"},
    }
    with patch.object(forge, "_create_agent_async", side_effect=[native, fallback]):
        await forge._run_agent("prompt", "system", response_schema=Decision, recursion_limit=7)

    assert native.ainvoke.await_args.kwargs["config"]["recursion_limit"] == 7
    assert fallback.ainvoke.await_args.kwargs["config"]["recursion_limit"] == 7

    retry_graph = AsyncMock()
    retry_graph.ainvoke.side_effect = [
        TimeoutError("request timed out"),
        {"messages": [{"content": "done"}]},
    ]
    with (
        patch.object(forge, "_create_agent_async", return_value=retry_graph),
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock),
    ):
        await forge._run_agent("prompt", "system", recursion_limit=7)

    assert retry_graph.ainvoke.await_count == 2
    assert all(
        call.kwargs["config"]["recursion_limit"] == 7
        for call in retry_graph.ainvoke.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unstructured", "native", "fallback"])
async def test_real_repeating_tool_graph_exhausts_current_invocation_only(mode: str) -> None:
    forge = ForgeAgent()
    model = RepeatingToolModel()
    response_schema = Decision if mode != "unstructured" else None
    strategy = (
        ToolStrategy(Decision)
        if mode == "fallback"
        else ProviderStrategy(Decision)
        if mode == "native"
        else None
    )
    graph = create_agent(model, tools=[repeat_probe], response_format=strategy)
    native = AsyncMock()
    native.ainvoke.side_effect = ValueError("native format unsupported")
    created = [native, graph] if mode == "fallback" else [graph]

    with (
        patch.object(forge, "_create_agent_async", side_effect=created) as factory,
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(GraphRecursionError),
    ):
        await forge._run_agent(
            "repeat tool forever", "test", response_schema=response_schema, recursion_limit=4
        )

    assert model.calls > 0
    assert factory.await_count == len(created)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_deep_agent_graph_honors_invocation_limit_below_default() -> None:
    forge = ForgeAgent()
    model = RepeatingToolModel()
    graph = create_deep_agent(model=model, tools=[repeat_probe], system_prompt="Offline test")

    with (
        patch.object(forge, "_create_agent_async", return_value=graph) as factory,
        pytest.raises(GraphRecursionError),
    ):
        await forge._run_agent("repeat tool forever", "test", recursion_limit=4)

    assert model.calls > 0
    factory.assert_awaited_once()
