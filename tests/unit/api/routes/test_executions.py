from unittest.mock import AsyncMock

import pytest

from forge.api.routes.executions import load_execution_read_model


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
