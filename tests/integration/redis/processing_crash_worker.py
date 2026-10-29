"""Subprocess fixture: terminate at durable command handoff boundaries."""

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from redis.asyncio import Redis

from forge.domain import WorkflowCommand, WorkflowCommandType, WorkflowIdentity
from forge.models.events import EventSource
from forge.orchestrator.event_adapters import CommandDecision, CommandDecisionStatus
from forge.orchestrator.ownership import OwnedRedis
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.consumer import QueueConsumer
from forge.queue.models import QueueMessage
from forge.reconciliation import ProcessingPhase, RedisObservationLedger


class State(TypedDict, total=False):
    ticket_key: str
    ticket_type: str
    current_node: str
    is_paused: bool
    approved: bool
    command_decisions: list[dict]
    command_handoffs: list[dict]


async def main(url: str, boundary: str) -> None:
    client = Redis.from_url(url, decode_responses=True)
    checkpoint_client = OwnedRedis.from_url(url)
    ticket = "CRASH-901"
    config = {"configurable": {"thread_id": ticket}}

    def terminate(point: str) -> None:
        if boundary == point:
            os._exit(86)  # Deliberately bypass cleanup to exercise real restart recovery.

    async def station(_state: State) -> dict:
        await client.incr("test:station-calls")
        terminate("mid_graph")
        return {"current_node": "waiting", "is_paused": True}

    builder = StateGraph(State)
    builder.add_node("gate", lambda _state: {"current_node": "gate", "is_paused": True})
    builder.add_node("station", station)
    builder.add_edge(START, "gate")
    builder.add_conditional_edges(
        "gate",
        lambda state: "station" if state.get("approved") else "end",
        {"station": "station", "end": END},
    )
    builder.add_edge("station", END)
    workflow = SimpleNamespace(name="feature_workflow", build_graph=lambda: builder)
    message = QueueMessage(
        message_id="1740000000000-0",
        event_id="approve-1",
        source=EventSource.JIRA,
        event_type="jira:issue_updated",
        ticket_key=ticket,
        payload={"issue": {"key": ticket, "fields": {"issuetype": {"name": "Feature"}}}},
    )
    decision = CommandDecision(
        CommandDecisionStatus.ACCEPTED,
        "approval crash test",
        WorkflowCommand(
            command_id="approve-crash-1",
            command_type=WorkflowCommandType.APPROVE,
            workflow=WorkflowIdentity(
                run_id=ticket, workflow_name="feature_workflow", definition_revision=1
            ),
            requested_at=message.timestamp,
        ),
    )
    try:
        async with AsyncRedisSaver.from_conn_string(redis_client=checkpoint_client) as saver:
            compiled = builder.compile(checkpointer=saver)
            if await saver.aget(config) is None:
                await compiled.ainvoke(
                    {"ticket_key": ticket, "ticket_type": "Feature", "approved": False}, config
                )
            ledger = RedisObservationLedger(client)
            original_accept = ledger.accept
            original_save = ledger.save_processing
            original_update = compiled.aupdate_state

            async def accept(*args, **kwargs):
                result = await original_accept(*args, **kwargs)
                terminate("accepted")
                return result

            async def save(record):
                await original_save(record)
                if record.phase is ProcessingPhase.PREPARED:
                    terminate("prepared")
                if record.phase is ProcessingPhase.APPLIED:
                    terminate("applied")

            async def update(*args, **kwargs):
                result = await original_update(*args, **kwargs)
                terminate("checkpoint")
                return result

            async def transition(_message, state, **_kwargs):
                await client.incr("test:transition-calls")
                return {**state, "approved": True, "is_paused": False}

            worker = OrchestratorWorker(consumer_name="crash-worker", observation_ledger=ledger)
            worker._checkpointer = saver
            invoke = worker._invoke_workflow

            async def invoke_then_terminate(*args, **kwargs):
                result = await invoke(*args, **kwargs)
                terminate("graph_done")
                return result

            consumer = QueueConsumer("crash-worker", redis_client=client)
            with (
                patch("forge.orchestrator.worker.ensure_skills", new=AsyncMock()),
                patch("forge.orchestrator.worker.interpret_event", return_value=decision),
                patch(
                    "forge.orchestrator.worker.validate_command_decision",
                    side_effect=lambda value, _state: value,
                ),
                patch.object(worker, "_resolve_custom_workflow", new=AsyncMock(return_value=None)),
                patch.object(worker.router, "resolve", return_value=workflow),
                patch.object(worker, "_get_compiled_workflow", return_value=compiled),
                patch.object(worker, "_apply_observation_transition", new=transition),
                patch.object(worker, "_invoke_workflow", new=invoke_then_terminate),
                patch.object(compiled, "aupdate_state", new=update),
                patch.object(ledger, "accept", new=accept),
                patch.object(ledger, "save_processing", new=save),
            ):
                async with consumer._distributed_ticket_lock(ticket):
                    await worker._process_workflow(message)
            records = await ledger.processing_for_ticket(ticket)
            assert len(records) == 1 and records[0].phase is ProcessingPhase.APPLIED
            assert (await compiled.aget_state(config)).values["current_node"] == "waiting"
    finally:
        await checkpoint_client.aclose()
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
