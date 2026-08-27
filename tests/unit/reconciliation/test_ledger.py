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
