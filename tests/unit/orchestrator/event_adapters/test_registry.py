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
