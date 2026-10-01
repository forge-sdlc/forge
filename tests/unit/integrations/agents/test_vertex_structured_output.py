"""Offline wire-level contract for Claude on Vertex structured output."""

import json
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest
from anthropic import transform_schema
from langchain.agents import create_agent
from langchain.agents.structured_output import (
    ProviderStrategy,
    StructuredOutputValidationError,
)
from langchain_core.tools import StructuredTool, tool
from langchain_google_vertexai.model_garden import ChatAnthropicVertex

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.structured_outputs import (
    STRUCTURED_RESPONSE_SCHEMAS,
    TaskGeneration,
)

MODEL = "claude-opus-4-8"
TASKS = {
    "tasks": [
        {
            "summary": "Offline task",
            "description": "Offline acceptance criteria",
            "repo": "example/repo",
        }
    ]
}


@tool
async def offline_probe() -> str:
    """Return a harmless constant."""
    return "Offline probe finished."


def _message(content: list[dict[str, Any]], stop_reason: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_offline",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _forge_model(sync_http: httpx.Client, async_http: httpx.AsyncClient) -> ChatAnthropicVertex:
    agent = ForgeAgent.__new__(ForgeAgent)
    agent.settings = MagicMock(
        llm_backend="vertex-ai",
        llm_model=MODEL,
        llm_max_tokens=1024,
        google_cloud_project="offline-project",
        google_cloud_location="global",
    )
    original_sync = anthropic.AnthropicVertex
    original_async = anthropic.AsyncAnthropicVertex

    def make_sync(**kwargs: Any) -> Any:
        return original_sync(
            **{**kwargs, "access_token": "offline-token", "http_client": sync_http}
        )

    def make_async(**kwargs: Any) -> Any:
        return original_async(
            **{**kwargs, "access_token": "offline-token", "http_client": async_http}
        )

    with (
        patch.object(anthropic, "AnthropicVertex", make_sync),
        patch.object(anthropic, "AsyncAnthropicVertex", make_async),
    ):
        return agent._create_model()


@pytest.fixture
async def vertex_model() -> Any:
    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Binding must not send a provider request")

    transport = httpx.MockTransport(reject_network)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            try:
                yield model
            finally:
                model.client.close()
                await model.async_client.close()


def _ordinary_tool(index: int, *, strict: bool | None = None) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "name": f"lookup_{index}",
        "description": "Look up a harmless value.",
        "input_schema": {
            "type": "object",
            "properties": {"filters": {"type": "object", "additionalProperties": True}},
            "required": ["filters"],
        },
    }
    if strict is not None:
        definition["strict"] = strict
    return definition


def _lookup_tool(index: int) -> StructuredTool:
    async def lookup(filters: dict[str, Any]) -> str:
        """Return a harmless lookup value."""
        return str(filters)

    return StructuredTool.from_function(
        coroutine=lookup,
        name=f"lookup_{index}",
        description="Return a harmless lookup value.",
    )


@pytest.mark.asyncio
async def test_many_ordinary_tools_keep_open_dictionary_inputs(vertex_model: Any) -> None:
    definitions = [_ordinary_tool(i) for i in range(26)]
    original = deepcopy(definitions)
    bound = vertex_model.bind_tools(
        definitions,
        strict=True,
        **ProviderStrategy(TaskGeneration).to_model_kwargs(),
    )

    assert len(bound.kwargs["tools"]) == 26
    assert all("strict" not in tool for tool in bound.kwargs["tools"])
    assert bound.kwargs["tools"][0]["input_schema"]["properties"]["filters"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert definitions == original


@pytest.mark.asyncio
async def test_more_than_twenty_explicit_strict_tools_are_rejected(vertex_model: Any) -> None:
    with pytest.raises(ValueError, match="20 strict tools"):
        vertex_model.bind_tools(
            [_ordinary_tool(i, strict=True) for i in range(21)],
            strict=True,
            **ProviderStrategy(TaskGeneration).to_model_kwargs(),
        )


@pytest.mark.asyncio
async def test_explicit_strict_tool_must_keep_open_dictionary_arguments(vertex_model: Any) -> None:
    with pytest.raises(ValueError, match="strict tool.*input schema"):
        vertex_model.bind_tools([_ordinary_tool(1, strict=True)])


@pytest.mark.asyncio
async def test_explicit_strict_marker_in_tool_extras_is_preserved(vertex_model: Any) -> None:
    opted_in = deepcopy(offline_probe)
    opted_in.extras = {
        "strict": True,
        "provider_tool_definition": {
            "name": "offline_probe",
            "description": "Return a harmless constant.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }

    bound = vertex_model.bind_tools([opted_in], strict=False)

    assert bound.kwargs["tools"][0]["strict"] is True


@pytest.mark.asyncio
async def test_supported_server_tool_keeps_provider_definition(vertex_model: Any) -> None:
    server_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}

    bound = vertex_model.bind_tools([server_tool], strict=True)

    assert bound.kwargs["tools"] == [server_tool]


@pytest.mark.asyncio
async def test_unrepresentable_server_tool_fails_locally(vertex_model: Any) -> None:
    with pytest.raises(ValueError, match="Unsupported Vertex Claude tool"):
        vertex_model.bind_tools([{"type": "unknown_server_tool", "name": "unknown"}])


@pytest.mark.asyncio
async def test_openai_function_tool_definition_remains_ordinary(vertex_model: Any) -> None:
    definition = {
        "type": "function",
        "function": {
            "name": "open_lookup",
            "description": "Look up a harmless value.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
            },
        },
    }

    bound = vertex_model.bind_tools([definition], strict=True)

    assert bound.kwargs["tools"][0]["name"] == "open_lookup"
    assert "strict" not in bound.kwargs["tools"][0]


@pytest.mark.asyncio
async def test_openai_function_per_tool_strict_marker_is_preserved(vertex_model: Any) -> None:
    definition = {
        "type": "function",
        "function": {
            "name": "fixed_lookup",
            "description": "Look up one value.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }

    bound = vertex_model.bind_tools([definition])

    assert bound.kwargs["tools"][0]["strict"] is True


@pytest.mark.asyncio
async def test_recursive_explicit_strict_schema_fails_locally(vertex_model: Any) -> None:
    recursive_tool = {
        "name": "walk_tree",
        "description": "Walk a tree.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"root": {"$ref": "#/$defs/Node"}},
            "required": ["root"],
            "additionalProperties": False,
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                    "additionalProperties": False,
                }
            },
        },
    }

    with pytest.raises(ValueError, match="recursive strict tool"):
        vertex_model.bind_tools([recursive_tool])


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_strict", [None, False, True])
async def test_binding_wide_strict_never_changes_ordinary_tools(
    vertex_model: Any, binding_strict: bool | None
) -> None:
    bound = vertex_model.bind_tools([_ordinary_tool(1)], strict=binding_strict)

    assert "strict" not in bound.kwargs
    assert "strict" not in bound.kwargs["tools"][0]


@pytest.mark.asyncio
async def test_compatible_explicit_strict_tool_is_preserved(vertex_model: Any) -> None:
    definition = {
        "name": "fixed_lookup",
        "description": "Look up one value.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    bound = vertex_model.bind_tools([definition], strict=True)

    assert bound.kwargs["tools"][0]["strict"] is True
    assert bound.kwargs["tools"][0]["input_schema"]["properties"]["key"]["type"] == "string"


@pytest.mark.asyncio
async def test_existing_output_configuration_merges_without_mutation(vertex_model: Any) -> None:
    response_format = ProviderStrategy(TaskGeneration).to_model_kwargs()["response_format"]
    original = deepcopy(response_format)
    output_config = {"effort": "low"}

    bound = vertex_model.bind_tools(
        [],
        response_format=response_format,
        output_config=output_config,
        tool_choice="auto",
    )

    assert bound.kwargs["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": transform_schema(TaskGeneration)},
    }
    assert bound.kwargs["tool_choice"] == {"type": "auto"}
    assert "response_format" not in bound.kwargs
    assert response_format == original
    assert output_config == {"effort": "low"}


@pytest.mark.asyncio
async def test_identical_existing_format_and_repeated_binding_do_not_mutate(
    vertex_model: Any,
) -> None:
    definition = _ordinary_tool(1, strict=False)
    original = deepcopy(definition)
    response_format = ProviderStrategy(TaskGeneration).to_model_kwargs()["response_format"]
    native_format = {"type": "json_schema", "schema": transform_schema(TaskGeneration)}

    first = vertex_model.bind_tools(
        [definition], response_format=response_format, output_config={"format": native_format}
    )
    second = vertex_model.bind_tools([definition], response_format=response_format)

    assert first.kwargs["output_config"]["format"] == native_format
    assert second.kwargs["tools"] == first.kwargs["tools"]
    assert first.kwargs["tools"][0]["strict"] is False
    assert definition == original


@pytest.mark.asyncio
async def test_unstructured_binding_omits_native_output_config(vertex_model: Any) -> None:
    bound = vertex_model.bind_tools([])

    assert bound.kwargs["tools"] == []
    assert "output_config" not in bound.kwargs


@pytest.mark.asyncio
async def test_conflicting_existing_output_format_fails(vertex_model: Any) -> None:
    with pytest.raises(ValueError, match="Conflicting response_format"):
        vertex_model.bind_tools(
            [],
            **ProviderStrategy(TaskGeneration).to_model_kwargs(),
            output_config={"format": {"type": "other"}},
        )


@pytest.mark.asyncio
async def test_unsupported_response_format_fails_locally(vertex_model: Any) -> None:
    with pytest.raises(ValueError, match="Unsupported Vertex Claude response_format"):
        vertex_model.bind_tools([], response_format={"type": "json_object"})


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", list(STRUCTURED_RESPONSE_SCHEMAS.values()))
async def test_every_host_response_schema_uses_native_vertex_format(
    vertex_model: Any, schema: Any
) -> None:
    bound = vertex_model.bind_tools([], **ProviderStrategy(schema).to_model_kwargs())

    assert bound.kwargs["output_config"]["format"] == {
        "type": "json_schema",
        "schema": transform_schema(schema),
    }
    assert "response_format" not in bound.kwargs


@pytest.mark.asyncio
async def test_unadapted_library_class_still_rejects_provider_strategy_before_http() -> None:
    requests: list[httpx.Request] = []

    def reject_network(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("The unadapted class should fail before HTTP")

    transport = httpx.MockTransport(reject_network)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = ChatAnthropicVertex(
                model_name=MODEL,
                project="offline-project",
                location="global",
                access_token="offline-token",
                http_client=sync_http,
                async_http_client=async_http,
                max_retries=0,
            )
            try:
                graph = create_agent(model, response_format=ProviderStrategy(TaskGeneration))
                with pytest.raises(TypeError, match="unexpected keyword argument 'strict'"):
                    await graph.ainvoke({"messages": [("user", "Return tasks")]})
            finally:
                model.client.close()
                await model.async_client.close()

    assert not requests


@pytest.mark.asyncio
async def test_forge_vertex_provider_strategy_preserves_ordinary_tool_and_final_json() -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert "strict" not in payload
        assert "response_format" not in payload
        assert payload["output_config"]["format"] == {
            "type": "json_schema",
            "schema": transform_schema(TaskGeneration),
        }
        assert len(payload["tools"]) == 27
        assert payload["tools"][0]["name"] == "offline_probe"
        assert all("strict" not in entry for entry in payload["tools"])
        assert payload["tools"][1]["input_schema"]["properties"]["filters"]["type"] == "object"
        if len(requests) == 1:
            return _message(
                [{"type": "tool_use", "id": "probe", "name": "offline_probe", "input": {}}],
                "tool_use",
            )
        assert len(requests) == 2
        assert "Offline probe finished." in json.dumps(payload["messages"])
        return _message([{"type": "text", "text": json.dumps(TASKS)}], "end_turn")

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.max_retries = 0
            try:
                graph = create_agent(
                    model,
                    tools=[offline_probe, *[_lookup_tool(i) for i in range(26)]],
                    response_format=ProviderStrategy(TaskGeneration),
                )
                result = await graph.ainvoke(
                    {"messages": [("user", "Run offline probe before final tasks")]},
                    config={"recursion_limit": 12},
                )
            finally:
                model.client.close()
                await model.async_client.close()

    assert result["structured_response"] == TaskGeneration.model_validate(TASKS)
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_text", "stop_reason"),
    [
        (json.dumps({"tasks": [{**TASKS["tasks"][0], "summary": ""}]}), "end_turn"),
        (
            json.dumps({"tasks": [{k: v for k, v in TASKS["tasks"][0].items() if k != "repo"}]}),
            "end_turn",
        ),
        (json.dumps({"tasks": [{**TASKS["tasks"][0], "repo": "invalid"}]}), "end_turn"),
        (json.dumps({"tasks": [{**TASKS["tasks"][0], "unknown": 1}]}), "end_turn"),
        ('{"tasks":', "max_tokens"),
        ("I cannot comply", "refusal"),
    ],
)
async def test_native_graph_rejects_invalid_final_task_json(
    response_text: str, stop_reason: str
) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _message([{"type": "text", "text": response_text}], stop_reason)

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.max_retries = 0
            try:
                graph = create_agent(model, response_format=ProviderStrategy(TaskGeneration))
                with pytest.raises(StructuredOutputValidationError):
                    await graph.ainvoke(
                        {"messages": [("user", "Return tasks")]},
                        config={"recursion_limit": 8},
                    )
            finally:
                model.client.close()
                await model.async_client.close()

    assert len(requests) == 1
