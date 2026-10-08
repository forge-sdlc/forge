"""Offline contracts for an explicit, locally validated Vertex tool-output mode."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from langchain.agents.structured_output import (
    MultipleStructuredOutputsError,
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)

from forge.integrations.agents.agent import ForgeAgent
from forge.integrations.agents.structured_outputs import (
    ArtifactDocument,
    AutomatedReviewTriage,
    EpicDecomposition,
    ProposalReviewTriage,
    TaskGeneration,
)
from forge.models.model_policy import ResolvedModelTarget
from tests.unit.integrations.agents.test_structured_output import _vertex_error
from tests.unit.integrations.agents.test_vertex_structured_output import (
    MODEL,
    TASKS,
    _forge_model,
    _lookup_tool,
    _message,
    _ordinary_tool,
)


def _agent(strategy: str = "tool") -> ForgeAgent:
    forge = ForgeAgent.__new__(ForgeAgent)
    forge.settings = MagicMock(
        llm_backend="vertex-ai",
        llm_model=MODEL,
        vertex_structured_output_strategy=strategy,
        agent_recursion_limit=12,
    )
    forge._checkpointer = None
    return forge


@pytest.mark.asyncio
@pytest.mark.parametrize("named_target", [False, True])
async def test_tool_mode_is_selected_before_first_invocation(named_target: bool) -> None:
    forge = _agent()
    target = None
    if named_target:
        forge.settings.llm_backend = "anthropic"
        target = ResolvedModelTarget(
            connection="offline-vertex",
            model=MODEL,
            backend="vertex-ai",
            project="offline-project",
            policy_key="generate_tasks",
            policy_source="global",
        )
    graph = AsyncMock()
    graph.ainvoke.return_value = {"structured_response": TASKS}
    with patch.object(forge, "_create_agent_async", return_value=graph) as create:
        result = await forge._run_agent(
            "Return tasks", "Offline system", response_schema=TaskGeneration, model_target=target
        )
    strategy = create.call_args.kwargs["response_format"]
    assert isinstance(strategy, ToolStrategy)
    assert strategy.handle_errors is False
    assert result == TaskGeneration.model_validate(TASKS)
    assert create.await_count == graph.ainvoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "model", "strategy"),
    [
        ("vertex-ai", MODEL, "native"),
        ("anthropic", MODEL, "tool"),
        ("vertex-ai", "gemini-3.5-flash", "tool"),
        ("google-genai", "gemini-3.5-flash", "tool"),
    ],
)
async def test_other_backends_and_native_default_keep_provider_strategy(
    backend: str, model: str, strategy: str
) -> None:
    forge = _agent(strategy)
    forge.settings.llm_backend = backend
    forge.settings.llm_model = model
    graph = AsyncMock()
    graph.ainvoke.return_value = {"structured_response": TASKS}
    with patch.object(forge, "_create_agent_async", return_value=graph) as create:
        result = await forge._run_agent("Return tasks", "System", response_schema=TaskGeneration)
    assert isinstance(create.call_args.kwargs["response_format"], ProviderStrategy)
    assert result == TaskGeneration.model_validate(TASKS)


@pytest.mark.asyncio
async def test_unstructured_vertex_call_has_no_output_strategy() -> None:
    forge = _agent()
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": []}
    with patch.object(forge, "_create_agent_async", return_value=graph) as create:
        assert await forge._run_agent("Question", "System") == ""
    assert create.call_args.kwargs["response_format"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _vertex_error(400, "output_config.format is not supported for this model"),
        _vertex_error(400, "structured_outputs is disabled by project policy"),
        _vertex_error(403, "Permission denied"),
    ],
)
async def test_tool_mode_terminal_errors_do_not_rebuild_or_downgrade(error: Exception) -> None:
    forge = _agent()
    graph = AsyncMock()
    graph.ainvoke.side_effect = error
    with (
        patch.object(forge, "_create_agent_async", return_value=graph) as create,
        patch("forge.integrations.agents.agent.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(type(error)),
    ):
        await forge._run_agent("Return tasks", "System", response_schema=TaskGeneration)
    assert create.await_count == graph.ainvoke.await_count == 1
    sleep.assert_not_awaited()


VALID_OUTPUTS = [
    (ArtifactDocument, {"content": "Offline document", "repositories": ["example/repo"]}),
    (
        EpicDecomposition,
        {"epics": [{"summary": "Epic", "plan": "Offline plan", "repo": "example/repo"}]},
    ),
    (TaskGeneration, TASKS),
    (AutomatedReviewTriage, {"verdict": "satisfied", "reason": "Offline review"}),
    (
        ProposalReviewTriage,
        {
            "decisions": [
                {"thread_id": "offline-thread", "disposition": "reply", "response": "Offline reply"}
            ]
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("schema", "output"), VALID_OUTPUTS)
async def test_real_forge_graph_uses_non_strict_tools_and_validates_final_output(
    tmp_path: Any, schema: Any, output: dict[str, Any]
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "format" in payload.get("output_config", {}):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "structured_outputs is disabled by project policy",
                    }
                },
            )
        assert "response_format" not in payload
        assert "strict" not in payload
        assert "format" not in payload.get("output_config", {})
        assert all(entry.get("strict") is not True for entry in payload["tools"])
        lookups = [entry for entry in payload["tools"] if entry["name"].startswith("lookup_")]
        assert len(lookups) == 26
        assert lookups[0]["input_schema"]["properties"]["filters"]["additionalProperties"] is True
        if len(requests) == 1:
            return _message(
                [
                    {
                        "type": "tool_use",
                        "id": "lookup",
                        "name": "lookup_0",
                        "input": {"filters": {"key": "value"}},
                    }
                ],
                "tool_use",
            )
        assert len(requests) == 2
        assert "value" in json.dumps(payload["messages"])
        return _message(
            [{"type": "tool_use", "id": "final", "name": schema.__name__, "input": output}],
            "tool_use",
        )

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.max_retries = 0
            model.client.max_retries = model.async_client.max_retries = 0
            forge = _agent()
            try:
                with (
                    patch.object(forge, "_create_model", return_value=model),
                    patch.object(forge, "_get_root_dir", return_value=tmp_path),
                    patch.object(forge, "_get_skill_paths", return_value=[]),
                    patch.object(
                        forge,
                        "_load_mcp_tools",
                        new=AsyncMock(return_value=[_lookup_tool(i) for i in range(26)]),
                    ),
                ):
                    result = await forge._run_agent(
                        "Run lookup then return output", "System", response_schema=schema
                    )
            finally:
                model.client.close()
                await model.async_client.close()
    assert result == schema.model_validate(output)
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid", "multiple", "missing"])
async def test_tool_output_errors_stop_without_schema_repair_or_strategy_retry(
    tmp_path: Any, failure: str
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) == 1, "Invalid output must not cause another provider request"
        if failure == "missing":
            return _message([{"type": "text", "text": "No final tool output"}], "end_turn")
        content = [
            {
                "type": "tool_use",
                "id": "final",
                "name": "TaskGeneration",
                "input": {"tasks": []} if failure == "invalid" else TASKS,
            }
        ]
        if failure == "multiple":
            content.append({**content[0], "id": "second"})
        return _message(content, "tool_use")

    error = {
        "invalid": StructuredOutputValidationError,
        "multiple": MultipleStructuredOutputsError,
        "missing": ValueError,
    }[failure]
    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.max_retries = 0
            model.client.max_retries = model.async_client.max_retries = 0
            forge = _agent()
            try:
                with (
                    patch.object(forge, "_create_model", return_value=model),
                    patch.object(forge, "_get_root_dir", return_value=tmp_path),
                    patch.object(forge, "_get_skill_paths", return_value=[]),
                    patch.object(forge, "_load_mcp_tools", new=AsyncMock(return_value=[])),
                    pytest.raises(error),
                ):
                    await forge._run_agent("Return tasks", "System", response_schema=TaskGeneration)
            finally:
                model.client.close()
                await model.async_client.close()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_explicit_strict_tools_fail_before_http_in_tool_mode(tmp_path: Any) -> None:
    requests = []

    def reject_network(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Explicit strict tool rejection must happen before HTTP")

    transport = httpx.MockTransport(reject_network)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.max_retries = 0
            model.client.max_retries = model.async_client.max_retries = 0
            forge = _agent()
            strict_tool = _lookup_tool(0)
            strict_tool.extras = {
                "strict": True,
                "provider_tool_definition": _ordinary_tool(0, strict=True),
            }
            try:
                with (
                    patch.object(forge, "_create_model", return_value=model),
                    patch.object(forge, "_get_root_dir", return_value=tmp_path),
                    patch.object(forge, "_get_skill_paths", return_value=[]),
                    patch.object(
                        forge, "_load_mcp_tools", new=AsyncMock(return_value=[strict_tool])
                    ),
                    pytest.raises(ValueError, match="strict tools.*tool.*mode"),
                ):
                    await forge._run_agent("Return tasks", "System", response_schema=TaskGeneration)
            finally:
                model.client.close()
                await model.async_client.close()
    assert not requests


@pytest.mark.asyncio
async def test_provider_definition_strict_marker_is_rejected_in_tool_mode() -> None:
    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Tool binding must never contact a provider")

    transport = httpx.MockTransport(reject_network)
    with httpx.Client(transport=transport) as sync_http:
        async with httpx.AsyncClient(transport=transport) as async_http:
            model = _forge_model(sync_http, async_http)
            model.tool_output_only = True
            strict_tool = _lookup_tool(0)
            strict_tool.extras = {
                "provider_tool_definition": {
                    "name": "fixed_lookup",
                    "description": "Return a harmless value.",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            }
            try:
                with pytest.raises(ValueError, match="strict tools.*tool.*mode"):
                    model.bind_tools([strict_tool])
            finally:
                model.client.close()
                await model.async_client.close()
