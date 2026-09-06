from datetime import UTC, datetime

import pytest

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
from forge.models.events import EventSource
from forge.models.workflow import TicketType
from forge.orchestrator.event_adapters import create_default_event_adapter_registry
from forge.orchestrator.event_adapters.jira import JiraEventAdapter
from forge.orchestrator.event_adapters.registry import EventAdapterRegistry
from forge.orchestrator.event_adapters.source_control import extract_change_request_url
from forge.queue.models import QueueMessage, normalized_event_to_dict
from forge.reconciliation import InMemoryObservationLedger, ObservationDisposition


def _message(
    *,
    source: EventSource,
    payload: dict | None = None,
    normalized_event: dict | None = None,
    ticket_key: str = "FORGE-42",
) -> QueueMessage:
    return QueueMessage(
        message_id="1-0",
        event_id="delivery-42",
        source=source,
        event_type="updated",
        ticket_key=ticket_key,
        payload=payload or {},
        normalized_event=normalized_event,
        timestamp=datetime(2026, 8, 27, tzinfo=UTC),
    )


def _source_control_event() -> NormalizedEvent:
    repo = RepositoryRef(
        id="acme/api",
        provider=Provider.GITHUB,
        connection="public",
        namespace="acme/api",
        default_branch="main",
        change_request_mode="direct",
    )
    return NormalizedEvent(
        id="delivery-42",
        kind=EventKind.CR_UPDATED,
        repo_ref=repo,
        actor=Actor(login="octocat", is_bot=False),
        received_at=datetime(2026, 8, 27, tzinfo=UTC),
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="public", repository_id="acme/api", native_id=17
            ),
            url="https://github.com/acme/api/pull/17",
            title="Change",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
            head_sha="abc123",
        ),
    )


def test_default_registry_adapts_jira_without_provider_clients() -> None:
    message = _message(
        source=EventSource.JIRA,
        payload={
            "issue": {
                "key": "FORGE-42",
                "fields": {"issuetype": {"name": "Feature"}},
            },
            "changelog": {"items": []},
        },
    )

    adapted = create_default_event_adapter_registry().adapt(message)

    assert adapted.ticket_type is TicketType.FEATURE
    assert adapted.observation.resource.external_id == "FORGE-42"
    assert adapted.observation.facts["event_type"] == "updated"


def test_jira_issue_revision_is_shared_when_delivery_ids_differ() -> None:
    payload = {
        "issue": {
            "key": "FORGE-42",
            "fields": {
                "issuetype": {"name": "Feature"},
                "updated": "2026-08-27T10:00:00.000+0000",
            },
        },
        "changelog": {"items": [{"field": "labels", "toString": "forge:managed"}]},
    }
    webhook = _message(source=EventSource.JIRA, payload=payload)
    poller = _message(source=EventSource.JIRA, payload=payload)
    poller.event_id = "poller-delivery-42"

    adapter = JiraEventAdapter()
    webhook_observation = adapter.adapt(webhook).observation
    poller_observation = adapter.adapt(poller).observation

    assert webhook_observation.resource_revision == "updated:2026-08-27T10:00:00.000+0000"
    assert webhook_observation.revision_order is not None
    assert webhook_observation.delivery_identity == poller_observation.delivery_identity


def test_jira_comment_id_wins_over_issue_revision_for_cross_source_replay() -> None:
    webhook_payload = {
        "issue": {
            "key": "FORGE-42",
            "fields": {
                "issuetype": {"name": "Feature"},
                "updated": "2026-08-27T10:00:00.000+0000",
            },
        },
        "comment": {"id": "10042", "body": "Please revise"},
    }
    poller_payload = {
        **webhook_payload,
        "issue": {
            **webhook_payload["issue"],
            "fields": {
                **webhook_payload["issue"]["fields"],
                "updated": "2026-08-27T10:01:00.000+0000",
            },
        },
    }
    adapter = JiraEventAdapter()
    webhook = adapter.adapt(_message(source=EventSource.JIRA, payload=webhook_payload)).observation
    poller = adapter.adapt(_message(source=EventSource.JIRA, payload=poller_payload)).observation

    assert webhook.resource_revision == "comment:10042"
    assert webhook.delivery_identity == poller.delivery_identity


def test_jira_comment_created_timestamp_orders_comments_without_issue_updated() -> None:
    payload = {
        "issue": {"key": "FORGE-42", "fields": {"issuetype": {"name": "Feature"}}},
        "comment": {"id": "10042", "created": "2026-08-27T10:01:00.000+0000"},
    }

    adapted = JiraEventAdapter().adapt(_message(source=EventSource.JIRA, payload=payload))

    assert adapted.observation.resource_revision == "comment:10042"
    assert adapted.observation.revision_order is not None


@pytest.mark.asyncio
async def test_rich_webhook_and_minimal_poller_facts_deduplicate_same_issue_revision() -> None:
    rich_payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "id": "10042",
            "key": "FORGE-42",
            "fields": {
                "issuetype": {"name": "Feature", "id": "10001"},
                "status": {"name": "In Progress", "id": "3"},
                "labels": ["forge:managed", "forge:pending"],
                "summary": "A richer provider issue",
                "description": {"type": "doc", "content": []},
                "updated": "2026-08-27T10:00:00.000+0000",
            },
        },
        "changelog": {"id": "history-1", "items": [{"field": "labels"}]},
        "user": {"accountId": "provider-user", "displayName": "Provider"},
    }
    minimal_payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "FORGE-42",
            "fields": {
                "issuetype": {"name": "Feature"},
                "status": {"name": "In Progress"},
                "labels": ["forge:managed", "forge:pending"],
                "updated": "2026-08-27T10:00:00.000+0000",
            },
        },
    }
    adapter = JiraEventAdapter()
    webhook_message = _message(source=EventSource.JIRA, payload=rich_payload)
    poller_message = _message(source=EventSource.JIRA, payload=minimal_payload)
    poller_message.event_id = "poller-delivery-42"
    webhook = adapter.adapt(webhook_message).observation
    poller = adapter.adapt(poller_message).observation

    assert webhook.facts == poller.facts
    assert webhook.delivery_identity == poller.delivery_identity
    ledger = InMemoryObservationLedger()
    assert (await ledger.record(webhook)).disposition is ObservationDisposition.ACCEPTED
    assert (await ledger.record(poller)).disposition is ObservationDisposition.DUPLICATE


def test_child_jira_event_rerouted_to_parent_does_not_start_child_workflow() -> None:
    message = _message(
        source=EventSource.JIRA,
        payload={
            "source_ticket_key": "FORGE-43",
            "issue": {
                "key": "FORGE-43",
                "fields": {"issuetype": {"name": "Task"}},
            },
        },
    )

    adapted = create_default_event_adapter_registry().adapt(message)

    assert adapted.ticket_type is TicketType.UNKNOWN


def test_default_registry_adapts_normalized_source_control_event() -> None:
    event = _source_control_event()
    message = _message(
        source=EventSource.SOURCE_CONTROL,
        normalized_event=normalized_event_to_dict(event),
        ticket_key="",
    )

    adapted = create_default_event_adapter_registry().adapt(message)

    assert adapted.normalized_event == event
    assert adapted.observation.resource.external_id == "acme/api#17"
    assert adapted.change_request_url == "https://github.com/acme/api/pull/17"
    assert adapted.requires_ticket_correlation is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"pull_request": {"html_url": "https://github.com/acme/api/pull/2"}},
            "https://github.com/acme/api/pull/2",
        ),
        (
            {"repository": {"full_name": "acme/api"}, "issue": {"number": 3}},
            "https://github.com/acme/api/pull/3",
        ),
        (
            {"review": {"pull_request_url": "https://api.github.com/repos/acme/api/pulls/4"}},
            "https://github.com/acme/api/pull/4",
        ),
    ],
)
def test_change_request_url_compatibility_shapes(payload: dict, expected: str) -> None:
    assert extract_change_request_url(payload) == expected


def test_registry_rejects_duplicate_source_registration() -> None:
    registry = EventAdapterRegistry()
    adapter = JiraEventAdapter()
    registry.register(adapter)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(adapter)
