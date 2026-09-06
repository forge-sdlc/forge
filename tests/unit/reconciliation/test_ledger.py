from datetime import UTC, datetime

import pytest

from forge.domain import Observation, ObservationSource, ResourceIdentity
from forge.reconciliation import (
    DriftClass,
    InMemoryObservationLedger,
    ObservationDisposition,
)


def observation(
    source: ObservationSource,
    order: int,
    *,
    status: str = "open",
) -> Observation:
    now = datetime.now(UTC)
    return Observation(
        observation_id=f"{source}-{order}",
        source=source,
        source_system="github",
        resource=ResourceIdentity(
            resource_type="change_request", external_id="17", namespace="org/repo"
        ),
        resource_revision=f"revision-{order}",
        revision_order=order,
        observed_at=now,
        received_at=now,
    facts={"status": status},
    )


def unversioned_observation(
    source: ObservationSource,
    observation_id: str,
    *,
    event_id: str | None = None,
) -> Observation:
    now = datetime.now(UTC)
    return Observation(
        observation_id=observation_id,
        source=source,
        source_system="jira",
        resource=ResourceIdentity(resource_type="issue", external_id="FORGE-17"),
        observed_at=now,
        received_at=now,
        facts={"event_type": "issue_updated"},
        correlation={"provider_event_id": event_id} if event_id else {},
    )


@pytest.mark.asyncio
async def test_webhook_and_poller_delivery_share_identity_and_deduplicate() -> None:
    ledger = InMemoryObservationLedger()
    webhook = observation(ObservationSource.WEBHOOK, 4)
    polled = observation(ObservationSource.POLLER, 4)

    first = await ledger.record(webhook)
    duplicate = await ledger.record(polled)

    assert webhook.delivery_identity == polled.delivery_identity
    assert first.disposition is ObservationDisposition.ACCEPTED
    assert duplicate.disposition is ObservationDisposition.DUPLICATE


@pytest.mark.asyncio
async def test_stale_delivery_cannot_overwrite_latest_projection() -> None:
    ledger = InMemoryObservationLedger()
    newest = observation(ObservationSource.WEBHOOK, 5, status="merged")
    stale = observation(ObservationSource.POLLER, 3)
    await ledger.record(newest)

    decision = await ledger.record(stale)

    assert decision.disposition is ObservationDisposition.STALE
    assert (await ledger.latest(stale)).latest.facts == {"status": "merged"}


@pytest.mark.asyncio
async def test_observation_history_can_be_rebuilt_by_workflow_run() -> None:
    ledger = InMemoryObservationLedger()
    current = observation(ObservationSource.WEBHOOK, 5)
    current = current.model_copy(
        update={"correlation": {"workflow_ticket_key": "FORGE-17"}}
    )
    older = observation(ObservationSource.POLLER, 3).model_copy(
        update={"correlation": {"workflow_ticket_key": "FORGE-17"}}
    )

    await ledger.record(current)
    await ledger.record(older)

    history = await ledger.history_for_run("FORGE-17")
    assert [item.disposition for item in history] == [
        ObservationDisposition.ACCEPTED,
        ObservationDisposition.STALE,
    ]


@pytest.mark.asyncio
async def test_same_revision_with_different_facts_requires_operator() -> None:
    ledger = InMemoryObservationLedger()
    await ledger.record(observation(ObservationSource.WEBHOOK, 5))

    decision = await ledger.record(observation(ObservationSource.POLLER, 5, status="closed"))

    assert decision.disposition is ObservationDisposition.CONFLICT
    assert decision.drift is DriftClass.OPERATOR_REQUIRED


@pytest.mark.asyncio
async def test_newer_observation_updates_projection_without_workflow_position() -> None:
    ledger = InMemoryObservationLedger()
    first = observation(ObservationSource.WEBHOOK, 1)
    await ledger.record(first)

    decision = await ledger.record(observation(ObservationSource.POLLER, 2, status="merged"))

    assert decision.disposition is ObservationDisposition.ACCEPTED
    assert decision.drift is DriftClass.AUTO_RECONCILABLE
    assert "current_node" not in decision.observation.facts


@pytest.mark.asyncio
async def test_external_observation_cannot_overwrite_workflow_position() -> None:
    ledger = InMemoryObservationLedger()
    incoming = observation(ObservationSource.POLLER, 1).model_copy(
        update={"facts": {"status": "merged", "current_node": "complete"}}
    )

    decision = await ledger.record(incoming)

    assert decision.disposition is ObservationDisposition.CONFLICT
    assert decision.drift is DriftClass.POLICY_BLOCKING
    assert await ledger.latest(incoming) is None


@pytest.mark.asyncio
async def test_unversioned_events_do_not_collapse_into_one_delivery() -> None:
    ledger = InMemoryObservationLedger()
    first = unversioned_observation(ObservationSource.WEBHOOK, "event-1")
    second = unversioned_observation(ObservationSource.WEBHOOK, "event-2")

    first_decision = await ledger.record(first)
    second_decision = await ledger.record(second)

    assert first.delivery_identity != second.delivery_identity
    assert first_decision.disposition is ObservationDisposition.ACCEPTED
    # Without an order or revision token, a second event cannot safely replace
    # the first projection; it is retained as an operator-visible conflict.
    assert second_decision.disposition is ObservationDisposition.CONFLICT
    assert second_decision.drift is DriftClass.OPERATOR_REQUIRED


@pytest.mark.asyncio
async def test_unversioned_provider_event_id_is_shared_across_sources() -> None:
    ledger = InMemoryObservationLedger()
    webhook = unversioned_observation(
        ObservationSource.WEBHOOK, "webhook-delivery", event_id="provider-event-7"
    )
    polled = unversioned_observation(
        ObservationSource.POLLER, "poll-delivery", event_id="provider-event-7"
    )

    assert webhook.delivery_identity == polled.delivery_identity
    assert (await ledger.record(webhook)).disposition is ObservationDisposition.ACCEPTED
    assert (await ledger.record(polled)).disposition is ObservationDisposition.DUPLICATE


@pytest.mark.asyncio
async def test_revision_order_and_token_mismatch_is_operator_conflict() -> None:
    ledger = InMemoryObservationLedger()
    await ledger.record(observation(ObservationSource.WEBHOOK, 4))
    incoming = observation(ObservationSource.POLLER, 4).model_copy(
        update={"resource_revision": "different-revision"}
    )

    decision = await ledger.record(incoming)

    assert decision.disposition is ObservationDisposition.CONFLICT
    assert decision.drift is DriftClass.OPERATOR_REQUIRED


@pytest.mark.asyncio
async def test_same_token_with_different_order_is_operator_conflict() -> None:
    ledger = InMemoryObservationLedger()
    await ledger.record(observation(ObservationSource.WEBHOOK, 4))
    incoming = observation(ObservationSource.POLLER, 5).model_copy(
        update={"resource_revision": "revision-4"}
    )

    decision = await ledger.record(incoming)

    assert decision.disposition is ObservationDisposition.CONFLICT
    assert decision.drift is DriftClass.OPERATOR_REQUIRED
