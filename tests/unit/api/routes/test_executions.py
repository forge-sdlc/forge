from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from forge.api.routes.executions import load_execution_read_model
from forge.read_models.models import TimelineEntry


@pytest.mark.asyncio
async def test_load_execution_read_model_uses_pinned_definition_and_effect_history() -> None:
    definition = {
        "apiVersion": "forge/v1",
        "kind": "Workflow",
        "metadata": {"name": "short-feature", "revision": 1},
        "spec": {
            "state": "feature",
            "entry": "generate_prd",
            "steps": {"generate_prd": {"next": "__end__"}},
        },
    }
    from forge.workflow.declarative.loader import load_workflow_value

    digest = load_workflow_value(definition).digest
    checkpointer = AsyncMock()
    checkpointer.aget.return_value = {
        "channel_values": {
            "thread_id": "FORGE-1",
            "ticket_key": "FORGE-1",
            "workflow_name": "short-feature",
            "workflow_revision": 1,
            "workflow_digest": digest,
            "workflow_definition": definition,
            "current_node": "generate_prd",
        }
    }
    journal = AsyncMock()
    journal.list_for_workflow.return_value = []

    model = await load_execution_read_model(
        "FORGE-1", checkpointer=checkpointer, effect_journal=journal
    )

    assert model is not None
    assert model.definition.available is True
    assert model.definition.manifest is not None
    assert model.definition.manifest["digest"] == digest
    journal.list_for_workflow.assert_awaited_once_with("FORGE-1")


@pytest.mark.asyncio
async def test_load_execution_read_model_returns_none_for_unknown_workflow() -> None:
    checkpointer = AsyncMock()
    checkpointer.aget.return_value = None

    assert await load_execution_read_model("MISSING-1", checkpointer=checkpointer) is None


@pytest.mark.asyncio
async def test_loader_rehydrates_observation_and_timeline_records_after_restart() -> None:
    checkpointer = AsyncMock()
    checkpointer.aget.return_value = {
        "channel_values": {
            "thread_id": "FORGE-2",
            "ticket_key": "FORGE-2",
            "current_node": "ci_evaluator",
            "observation_history": [],
        }
    }
    journal = AsyncMock()
    journal.list_for_workflow.return_value = []
    ledger = AsyncMock()
    ledger.history_for_run.return_value = [
        {
            "observation_id": "observation-1",
            "delivery_identity": "delivery-1",
            "disposition": "stale",
            "decided_at": "2026-08-28T11:58:00+00:00",
            "reason": "older provider revision",
        }
    ]
    timeline = AsyncMock()
    timeline.list.return_value = [
        TimelineEntry(
            event_id="operator-1",
            kind="operator_action",
            occurred_at=datetime(2026, 8, 28, 11, 59, tzinfo=UTC),
            status="accepted",
            summary="retry",
        )
    ]

    first = await load_execution_read_model(
        "FORGE-2",
        checkpointer=checkpointer,
        effect_journal=journal,
        observation_ledger=ledger,
        timeline_store=timeline,
    )
    # Simulate a process restart: all records are re-read from the durable
    # adapters rather than relying on in-process projection state.
    second = await load_execution_read_model(
        "FORGE-2",
        checkpointer=checkpointer,
        effect_journal=journal,
        observation_ledger=ledger,
        timeline_store=timeline,
    )

    assert first is not None and second is not None
    assert first.timeline == second.timeline
    assert [entry.kind for entry in first.timeline] == [
        "observation",
        "operator_action",
    ]
    ledger.history_for_run.assert_awaited_with("FORGE-2")
    timeline.list.assert_awaited_with("FORGE-2")
