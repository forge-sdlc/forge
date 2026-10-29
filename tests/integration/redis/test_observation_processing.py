"""Real Redis acceptance and restart coverage for workflow processing records."""

import pytest

from forge.models.events import EventSource
from forge.orchestrator.event_adapters import create_default_event_adapter_registry
from forge.queue.models import QueueMessage
from forge.reconciliation import ProcessingPhase, RedisObservationLedger


def _message(*, retry_count: int = 0) -> QueueMessage:
    return QueueMessage(
        message_id="1740000000000-0",
        event_id="provider-start-1",
        source=EventSource.JIRA,
        event_type="jira:issue_created",
        ticket_key="FORGE-901",
        payload={
            "issue": {
                "key": "FORGE-901",
                "fields": {"issuetype": {"name": "Feature"}},
            }
        },
        retry_count=retry_count,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_accepted_start_record_survives_ledger_restart_and_redelivery(redis_client):
    original = _message()
    first_ledger = RedisObservationLedger(redis_client)
    adapted = create_default_event_adapter_registry().adapt(original)
    decision, accepted = await first_ledger.accept(
        adapted.observation,
        original.ticket_key,
        {"message_id": original.message_id, **original.to_dict()},
    )
    assert decision.disposition.value == "accepted"
    assert accepted is not None and accepted.phase is ProcessingPhase.PENDING
    assert (
        await redis_client.zscore(
            "forge:observations:processing:pending", accepted.delivery_identity
        )
        is not None
    )

    # A new ledger models process restart after acceptance but before command
    # preparation. Duplicate acceptance must return the original saved ingress.
    restarted_ledger = RedisObservationLedger(redis_client)
    duplicate, recovered = await restarted_ledger.accept(
        adapted.observation,
        original.ticket_key,
        {"message_id": "redelivered", **original.increment_retry().to_dict()},
    )
    assert duplicate.disposition.value == "duplicate"
    assert recovered == accepted
    assert recovered is not None
    assert recovered.phase is ProcessingPhase.PENDING
    assert recovered.ingress_message["message_id"] == original.message_id
    assert (
        await redis_client.zscore(
            "forge:observations:processing:pending", accepted.delivery_identity
        )
        is not None
    )
