"""Real Redis tests for atomic ticket fencing at the checkpoint boundary."""

from typing import TypedDict

import pytest
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, StateGraph
from redis.asyncio.client import Pipeline

from forge.orchestrator.ownership import (
    OwnedRedis,
    TicketOwnership,
    TicketOwnershipLost,
    owned_transaction,
    ownership_scope,
)


async def test_direct_and_pipeline_writes_reject_replaced_owner(redis_client, redis_url):
    client = OwnedRedis.from_url(redis_url)
    await redis_client.set("ticket-lock", "a")
    owner = TicketOwnership(redis_client, "ticket-lock", "a")
    try:
        with ownership_scope(owner):
            await client.set("result", "first")
            async with client.pipeline(transaction=False) as pipeline:
                pipeline.json().set("checkpoint", "$", {"value": 1})
                pipeline.set("latest", "checkpoint")
                await redis_client.set("ticket-lock", "b")
                with pytest.raises(TicketOwnershipLost):
                    await pipeline.execute()
            with pytest.raises(TicketOwnershipLost):
                await client.set("result", "stale")
        assert await redis_client.get("result") == "first"
        assert await redis_client.exists("checkpoint", "latest") == 0
    finally:
        await client.aclose()


async def test_expired_owner_cannot_write(redis_client, redis_url):
    client = OwnedRedis.from_url(redis_url)
    try:
        with (
            ownership_scope(TicketOwnership(redis_client, "missing-lock", b"expired")),
            pytest.raises(TicketOwnershipLost),
        ):
            await client.set("forbidden", "value")
        assert not await redis_client.exists("forbidden")
    finally:
        await client.aclose()


async def test_ownership_change_between_check_and_exec_is_rejected(
    redis_client, redis_url, monkeypatch
):
    client = OwnedRedis.from_url(redis_url)
    await redis_client.set("ticket-lock", "a")
    original = Pipeline._execute_transaction
    replaced = False

    async def replace_before_exec(pipeline, *args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            await redis_client.set("ticket-lock", "b")
        return await original(pipeline, *args, **kwargs)

    monkeypatch.setattr(Pipeline, "_execute_transaction", replace_before_exec)
    try:
        with (
            ownership_scope(TicketOwnership(redis_client, "ticket-lock", "a")),
            pytest.raises(TicketOwnershipLost),
        ):
            await client.set("forbidden", "value")
        assert not await redis_client.exists("forbidden")
    finally:
        await client.aclose()


async def test_owned_transaction_supports_additional_watches(redis_client):
    await redis_client.set("ticket-lock", "a")
    with ownership_scope(TicketOwnership(redis_client, "ticket-lock", "a")):
        async with owned_transaction(redis_client) as pipeline:
            await pipeline.watch("processing")
            assert await pipeline.get("processing") is None
            pipeline.multi()
            pipeline.set("processing", "applied")
            await pipeline.execute()
    assert await redis_client.get("processing") == "applied"


async def test_real_graph_checkpoint_and_pending_writes_are_fenced(redis_client, redis_url):
    class State(TypedDict):
        count: int

    client = OwnedRedis.from_url(redis_url)
    try:
        async with AsyncRedisSaver.from_conn_string(redis_client=client) as saver:
            await saver.asetup()
            builder = StateGraph(State)
            builder.add_node("increment", lambda state: {"count": state["count"] + 1})
            builder.add_edge(START, "increment")
            builder.add_edge("increment", END)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "FORGE-1"}}
            await redis_client.set("ticket-lock", "a")
            owner = TicketOwnership(redis_client, "ticket-lock", "a")
            with ownership_scope(owner):
                assert (await graph.ainvoke({"count": 1}, config))["count"] == 2
                checkpoint = await saver.aget_tuple(config)
                assert checkpoint is not None
                await redis_client.set("ticket-lock", "b")
                with pytest.raises(TicketOwnershipLost):
                    await saver.aput_writes(checkpoint.config, [("count", 99)], "stale-task")
                with pytest.raises(TicketOwnershipLost):
                    await graph.aupdate_state(config, {"count": 99})
            assert (await graph.aget_state(config)).values["count"] == 2
    finally:
        await client.aclose()
