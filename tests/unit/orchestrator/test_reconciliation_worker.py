"""Worker ingress tests for source-independent reconciliation."""

from unittest.mock import AsyncMock, patch

import pytest

from forge.models.events import EventSource
from forge.orchestrator.event_adapters import create_default_event_adapter_registry
from forge.orchestrator.worker import OrchestratorWorker
from forge.queue.models import QueueMessage
from forge.reconciliation import InMemoryObservationLedger, ObservationDisposition


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
