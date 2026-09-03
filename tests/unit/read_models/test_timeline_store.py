import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from forge.read_models import (
    InMemoryExecutionTimelineStore,
    RedisExecutionTimelineStore,
    project_execution,
    rebuild_execution_timeline,
    timeline_entry,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


class _AtomicFakeRedis:
    """Tiny fake that implements the Lua append contract, not Redis itself."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.eval_calls = 0
        self.fail_once = False

    async def eval(self, _script: str, _key_count: int, event_key: str, run_key: str, value: str) -> int:
        self.eval_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated script interruption before commit")
        if event_key in self.values:
            return 0
        # This method is the atomic boundary in the fake: no await occurs
        # between marker creation and list append.
        self.values[event_key] = value
        self.lists.setdefault(run_key, []).append(value)
        return 1

    async def lrange(self, run_key: str, start: int, end: int) -> list[str]:
        values = self.lists.get(run_key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    async def rpush(self, *_args: object) -> None:
        raise AssertionError("timeline append must use the atomic Lua operation")


@pytest.mark.asyncio
async def test_timeline_store_is_idempotent_and_orders_replayed_records() -> None:
    store = InMemoryExecutionTimelineStore()
    late = timeline_entry(
        event_id="transition-1",
        kind="transition",
        occurred_at=NOW - timedelta(minutes=1),
        summary="entry → work",
    )
    early = timeline_entry(
        event_id="observation-1",
        kind="observation",
        occurred_at=NOW - timedelta(minutes=2),
        summary="accepted",
        status="accepted",
    )

    assert await store.append("RUN-1", late) is True
    assert await store.append("RUN-1", late) is False
    assert await store.append_many("RUN-1", [early, late]) == 1
    assert [item.event_id for item in await store.list("RUN-1")] == [
        "observation-1",
        "transition-1",
    ]


@pytest.mark.asyncio
async def test_redis_timeline_append_is_atomic_under_concurrent_duplicates() -> None:
    redis = _AtomicFakeRedis()
    store = RedisExecutionTimelineStore(redis)
    entry = timeline_entry(
        event_id="operator-1",
        kind="operator_action",
        occurred_at=NOW,
        summary="retry",
    )

    outcomes = await asyncio.gather(
        *(store.append("RUN-1", entry) for _ in range(8))
    )

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    assert len(await store.list("RUN-1")) == 1
    assert redis.eval_calls == 8


@pytest.mark.asyncio
async def test_redis_timeline_retry_after_script_interruption_is_complete() -> None:
    redis = _AtomicFakeRedis()
    redis.fail_once = True
    store = RedisExecutionTimelineStore(redis)
    entry = timeline_entry(
        event_id="operator-1",
        kind="operator_action",
        occurred_at=NOW,
        summary="retry",
    )

    with pytest.raises(RuntimeError):
        await store.append("RUN-1", entry)
    assert await store.list("RUN-1") == ()
    assert await store.append("RUN-1", entry) is True
    assert len(await store.list("RUN-1")) == 1


def test_projection_rebuilds_timeline_from_all_durable_record_categories() -> None:
    model = project_execution(
        {
            "ticket_key": "RUN-1",
            "current_node": "work",
            "observation_history": [
                {
                    "observation_id": "obs-1",
                    "disposition": "stale",
                    "decided_at": (NOW - timedelta(minutes=4)).isoformat(),
                    "reason": "older provider revision",
                }
            ],
            "command_decisions": [
                {
                    "decision_id": "command-1",
                    "decided_at": (NOW - timedelta(minutes=3)).isoformat(),
                    "status": "ignored",
                    "reason": "duplicate command",
                }
            ],
            "transition_history": [
                {
                    "transition_id": "transition-1",
                    "source": "entry",
                    "target": "work",
                    "occurred_at": (NOW - timedelta(minutes=2)).isoformat(),
                }
            ],
            "migration_history": [
                {
                    "migration_id": "migration-1",
                    "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
                    "status": "blocked",
                    "reason": "missing resume mapping",
                }
            ],
            "operator_actions": [
                {
                    "action_id": "operator-1",
                    "occurred_at": NOW.isoformat(),
                    "action": "retry",
                    "actor": "operator@example.test",
                }
            ],
        },
        now=NOW,
    )

    assert [entry.kind for entry in model.timeline] == [
        "observation",
        "command_decision",
        "transition",
        "migration",
        "operator_action",
    ]
    assert model.timeline[0].status == "stale"
    assert model.timeline[-1].details["actor"] == "operator@example.test"

    rebuilt = rebuild_execution_timeline(
        {
            "ticket_key": "RUN-1",
            "current_node": "work",
        },
        timeline_entries=tuple(reversed(model.timeline)),
    )
    assert rebuilt == tuple(sorted(model.timeline, key=lambda item: (
        item.occurred_at or datetime.min.replace(tzinfo=UTC), item.kind, item.event_id
    )))
