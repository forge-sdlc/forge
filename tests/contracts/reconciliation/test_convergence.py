"""Conformance tests for webhook/poller observation convergence.

The fixture contains provider revisions, while this module supplies the two
transport paths.  Keeping the provider revision data transport-neutral makes
it possible for forge-poller to run the same fixture once it emits the
versioned Observation envelope.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forge.domain import Observation, ObservationSource, WorkflowCommandType
from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
)
from forge.integrations.source_control.observations import normalized_event_to_observation
from forge.models.events import EventSource
from forge.orchestrator.event_adapters import (
    CommandDecisionStatus,
    create_default_event_adapter_registry,
    interpret_event,
)
from forge.queue.models import QueueMessage, normalized_event_to_dict
from forge.reconciliation import InMemoryObservationLedger, ObservationDisposition

FIXTURE = Path(__file__).parents[1] / "fixtures" / "reconciliation" / "source_control_sequence.json"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
WORKFLOW_STATE: dict[str, Any] = {
    "thread_id": "FORGE-42",
    "ticket_key": "FORGE-42",
    "workflow_name": "feature",
    "workflow_definition_revision": 3,
    "current_node": "human_review_gate",
}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _event(revision: dict[str, Any]) -> NormalizedEvent:
    resource = _fixture()["resource"]
    repo_id, number = resource["external_id"].split("#")
    return NormalizedEvent(
        id=revision["provider_event_id"],
        kind=EventKind(revision["facts"]["kind"]),
        repo_ref=RepositoryRef(
            id=repo_id,
            provider=Provider.GITHUB,
            connection=resource["namespace"],
            namespace=repo_id,
            default_branch="main",
            change_request_mode="direct",
        ),
        actor=Actor(login="alice", is_bot=False),
        received_at=NOW,
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection=resource["namespace"], repository_id=repo_id, native_id=number
            ),
            url=f"https://github.com/{repo_id}/pull/{number}",
            title="Conformance fixture",
            body="",
            state=ChangeRequestState(revision["facts"]["change_request_state"]),
            source_branch="feature",
            target_branch="main",
            head_sha=revision["resource_revision"],
        ),
    )


def _observation(
    revision: dict[str, Any], source: ObservationSource
) -> tuple[Observation, NormalizedEvent]:
    event = _event(revision)
    observation = normalized_event_to_observation(event, source=source).model_copy(
        update={"revision_order": revision["revision_order"]}
    )
    return observation, event


def _command(
    observation: Observation,
    event: NormalizedEvent,
    *,
    transport_event_id: str | None = None,
) -> tuple[str, str | None, str | None, dict[str, Any]]:
    transport_event_id = transport_event_id or event.id
    message = QueueMessage(
        message_id=f"message-{transport_event_id}",
        event_id=transport_event_id,
        source=EventSource.SOURCE_CONTROL,
        event_type=event.kind.value,
        ticket_key="FORGE-42",
        normalized_event=normalized_event_to_dict(event),
        timestamp=NOW,
    )
    adapted = create_default_event_adapter_registry().adapt(message)
    # Adaptation is deliberately shared; only the ingress source marker differs.
    adapted = replace(adapted, observation=observation)
    decision = interpret_event(message, adapted, WORKFLOW_STATE)
    return (
        decision.status.value,
        decision.command.command_id if decision.command else None,
        decision.command.command_type.value if decision.command else None,
        decision.command.arguments if decision.command else {},
    )


async def _replay(
    revisions: list[dict[str, Any]], sources: list[ObservationSource]
) -> dict[str, Any]:
    ledger = InMemoryObservationLedger()
    command_decisions: list[tuple[str, str | None, str | None, dict[str, Any]]] = []
    accepted_effects: list[str] = []
    delivery_dispositions: list[str] = []
    for revision, source in zip(revisions, sources, strict=True):
        observation, event = _observation(revision, source)
        reconciliation = await ledger.record(observation)
        delivery_dispositions.append(reconciliation.disposition.value)
        if reconciliation.disposition is ObservationDisposition.ACCEPTED:
            decision = _command(observation, event)
            command_decisions.append(decision)
            if decision[0] is CommandDecisionStatus.ACCEPTED.value and decision[2] is not None:
                accepted_effects.append(decision[2])

    latest = await ledger.latest(_observation(revisions[-1], sources[-1])[0])
    assert latest is not None
    return {
        "latest_revision": latest.latest.resource_revision,
        # Delivery history is intentionally omitted: a lost event or a
        # duplicate delivery may change that history while the workflow and
        # externally visible effect state must converge.
        "command_decisions": [
            decision
            for decision in command_decisions
            if decision[0] == CommandDecisionStatus.ACCEPTED.value
        ],
        "effects": accepted_effects,
        "delivery_dispositions": delivery_dispositions,
    }


def test_shared_fixture_is_versioned_and_has_provider_revision_identity() -> None:
    fixture = _fixture()
    assert fixture["schema_version"] == "1.0"
    assert fixture["resource"]["resource_type"] == "change_request"
    assert all(
        revision["provider_event_id"]
        and revision["resource_revision"]
        and revision["revision_order"] >= 0
        for revision in fixture["revisions"]
    )


@pytest.mark.asyncio
async def test_webhook_and_poller_paths_emit_equivalent_observations() -> None:
    revisions = _fixture()["revisions"]
    webhook, _ = _observation(revisions[1], ObservationSource.WEBHOOK)
    poller, _ = _observation(revisions[1], ObservationSource.POLLER)

    assert webhook.source is ObservationSource.WEBHOOK
    assert poller.source is ObservationSource.POLLER
    assert webhook.delivery_identity == poller.delivery_identity
    assert webhook.model_copy(update={"source": poller.source}) == poller


def test_transport_delivery_id_does_not_change_command_identity() -> None:
    """A poller retry must select the same command as its webhook counterpart."""
    revision = _fixture()["revisions"][1]
    webhook, event = _observation(revision, ObservationSource.WEBHOOK)
    poller, _ = _observation(revision, ObservationSource.POLLER)

    webhook_command = _command(webhook, event, transport_event_id="github-delivery-17")
    poller_command = _command(poller, event, transport_event_id="poller-delivery-17")

    assert webhook.delivery_identity == poller.delivery_identity
    assert webhook_command == poller_command


@pytest.mark.asyncio
async def test_lost_duplicate_stale_and_reordered_delivery_converges() -> None:
    revisions = _fixture()["revisions"]
    opened, merged = revisions
    expected = await _replay(
        revisions,
        [ObservationSource.WEBHOOK, ObservationSource.WEBHOOK],
    )

    # The opened event is lost; merge arrives through both paths, is replayed,
    # and the old opened revision is delivered after it.
    degraded = await _replay(
        [merged, merged, opened, merged],
        [
            ObservationSource.WEBHOOK,
            ObservationSource.POLLER,
            ObservationSource.POLLER,
            ObservationSource.WEBHOOK,
        ],
    )

    assert {key: expected[key] for key in ("latest_revision", "command_decisions", "effects")} == {
        key: degraded[key] for key in ("latest_revision", "command_decisions", "effects")
    }
    assert degraded["delivery_dispositions"] == ["accepted", "duplicate", "stale", "duplicate"]
    assert expected["effects"] == [WorkflowCommandType.APPROVE.value]


@pytest.mark.asyncio
async def test_fixture_expected_commands_match_both_ingress_sources() -> None:
    revisions = _fixture()["revisions"]
    for source in (ObservationSource.WEBHOOK, ObservationSource.POLLER):
        for revision in revisions:
            observation, event = _observation(revision, source)
            status, _command_id, command_type, _arguments = _command(observation, event)
            expected = revision["expected_command"]
            if expected is None:
                assert status == CommandDecisionStatus.IGNORED.value
            else:
                assert status == CommandDecisionStatus.ACCEPTED.value
                assert command_type == expected
