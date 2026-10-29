"""Worker ingress tests for source-independent reconciliation."""

import asyncio
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from forge.models.events import EventSource
from forge.orchestrator.event_adapters import create_default_event_adapter_registry
from forge.orchestrator.worker import OrchestratorWorker, _stop_effect_runner
from forge.queue.models import QueueMessage
from forge.reconciliation import InMemoryObservationLedger, ObservationDisposition, ProcessingPhase


def _jira_message() -> QueueMessage:
    return QueueMessage(
        message_id="message-1",
        event_id="provider-event-1",
        source=EventSource.JIRA,
        event_type="jira:issue_updated",
        ticket_key="FORGE-42",
        payload={
            "issue": {
                "key": "FORGE-42",
                "fields": {"issuetype": {"name": "Feature"}},
            }
        },
    )


@pytest.mark.asyncio
async def test_duplicate_observation_does_not_reinterpret_or_start_workflow() -> None:
    message = _jira_message()
    adapters = create_default_event_adapter_registry()
    observation = adapters.adapt(message).observation
    ledger = InMemoryObservationLedger()
    assert (await ledger.record(observation)).disposition is ObservationDisposition.ACCEPTED

    worker = OrchestratorWorker(consumer_name="test-worker", observation_ledger=ledger)
    with (
        patch("forge.orchestrator.worker.ensure_skills", new=AsyncMock()),
        patch("forge.orchestrator.worker.interpret_event") as interpret,
        patch.object(worker, "_invoke_workflow", new=AsyncMock()) as invoke,
    ):
        await worker._process_workflow(message)

    interpret.assert_not_called()
    invoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_blocked_predecessor_prevents_newer_observation_execution(legacy) -> None:
    ledger = InMemoryObservationLedger()
    worker = OrchestratorWorker(consumer_name="blocked-test", observation_ledger=ledger)
    original = _jira_message()
    original.payload["issue"]["fields"]["updated"] = "2026-10-29T10:00:00Z"
    observation = worker.event_adapters.adapt(original).observation
    if legacy:
        await ledger.record(observation)
    _, record = await ledger.accept(
        observation, original.ticket_key, {"message_id": original.message_id, **original.to_dict()}
    )
    assert record is not None
    if not legacy:
        await ledger.save_processing(record.model_copy(update={"phase": ProcessingPhase.BLOCKED}))
    newer = _jira_message()
    newer.message_id = "message-2"
    newer.event_id = "provider-event-2"
    newer.payload["issue"]["fields"]["updated"] = "2026-10-29T10:00:01Z"

    with (
        patch("forge.orchestrator.worker.ensure_skills", new=AsyncMock()) as sync_skills,
        patch("forge.orchestrator.worker.interpret_event") as interpret,
        patch.object(worker, "_resolve_custom_workflow", new=AsyncMock(return_value=None)),
        patch.object(worker.router, "resolve", return_value=SimpleNamespace(name="feature")),
        patch.object(
            worker,
            "_get_compiled_workflow",
            return_value=SimpleNamespace(aget_state=AsyncMock(return_value=None)),
        ),
        patch.object(
            worker,
            "_invoke_workflow",
            new=AsyncMock(return_value={"current_node": "waiting", "is_paused": True}),
        ) as invoke,
        pytest.raises(RuntimeError, match="earlier accepted observation .* is blocked"),
    ):
        await worker._process_workflow(newer)

    sync_skills.assert_not_awaited()
    interpret.assert_not_called()
    invoke.assert_not_awaited()
    pending = await ledger.pending_processing(ticket_key=original.ticket_key)
    assert [item.phase for item in pending] == [ProcessingPhase.BLOCKED, ProcessingPhase.PENDING]
    await ledger.save_processing(record.model_copy(update={"phase": ProcessingPhase.IGNORED}))
    assert [item.phase for item in await ledger.pending_processing()] == [ProcessingPhase.PENDING]


@pytest.mark.asyncio
async def test_checkpointed_handoff_resumes_graph_without_reapplying_transition() -> None:
    class State(TypedDict, total=False):
        ticket_key: str
        ticket_type: str
        current_node: str
        is_paused: bool
        approved: bool
        station_calls: int
        command_decisions: list[dict]
        command_handoffs: list[dict]

    def gate(_state: State) -> dict:
        return {"current_node": "gate", "is_paused": True}

    def route(state: State) -> str:
        return "station" if state.get("approved") else "end"

    def station(state: State) -> dict:
        return {
            "current_node": "waiting",
            "is_paused": True,
            "station_calls": state.get("station_calls", 0) + 1,
        }

    builder = StateGraph(State)
    builder.add_node("gate", gate)
    builder.add_node("station", station)
    builder.add_edge(START, "gate")
    builder.add_conditional_edges("gate", route, {"station": "station", "end": END})
    builder.add_edge("station", END)
    workflow = SimpleNamespace(name="feature_workflow", build_graph=lambda: builder)
    ledger = InMemoryObservationLedger()
    worker = OrchestratorWorker(consumer_name="handoff-test", observation_ledger=ledger)
    worker._checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "FORGE-42"}}
    compiled = worker._get_compiled_workflow(workflow)
    await compiled.ainvoke(
        {
            "ticket_key": "FORGE-42",
            "ticket_type": "Feature",
            "current_node": "entry",
            "is_paused": False,
            "approved": False,
            "station_calls": 0,
        },
        config=config,
    )
    message = _jira_message()
    message.ticket_key = "FORGE-42"

    async def apply_transition(_message, current_state, **_kwargs):
        return {
            **current_state,
            "approved": True,
            "is_paused": False,
            "current_node": "gate",
        }

    apply = AsyncMock(side_effect=apply_transition)
    fail_once = AsyncMock(side_effect=RuntimeError("crash after checkpoint handoff"))

    with (
        patch("forge.orchestrator.worker.ensure_skills", new=AsyncMock()),
        patch.object(worker, "_resolve_custom_workflow", new=AsyncMock(return_value=None)),
        patch.object(worker.router, "resolve", return_value=workflow),
        patch.object(worker, "_apply_observation_transition", new=apply),
        patch.object(worker, "_invoke_workflow", new=fail_once),
        pytest.raises(RuntimeError, match="crash after checkpoint handoff"),
    ):
        await worker._process_workflow(message)

    saved = await compiled.aget_state(config)
    assert saved.values["approved"] is True
    assert saved.values["command_handoffs"][0]["phase"] == "applied"
    prepared = (await ledger.processing_for_ticket("FORGE-42"))[0]
    assert prepared.phase.value == "prepared"
    assert prepared.command_decision is not None

    with (
        patch("forge.orchestrator.worker.ensure_skills", new=AsyncMock()),
        patch.object(worker, "_resolve_custom_workflow", new=AsyncMock(return_value=None)),
        patch.object(worker.router, "resolve", return_value=workflow),
        patch.object(worker, "_apply_observation_transition", new=apply),
    ):
        await worker._process_workflow(message)

    apply.assert_awaited_once()
    completed = await compiled.aget_state(config)
    assert completed.values["station_calls"] == 1
    assert (await ledger.get_processing(prepared.delivery_identity)).phase.value == "applied"


@pytest.mark.asyncio
async def test_effect_runner_shutdown_is_bounded() -> None:
    stop = asyncio.Event()
    started = asyncio.Event()

    async def run_forever(_stop):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(run_forever(stop))
    await started.wait()

    await _stop_effect_runner(stop, task, timeout=0.01)

    assert stop.is_set()
    assert task.cancelling() == 1
