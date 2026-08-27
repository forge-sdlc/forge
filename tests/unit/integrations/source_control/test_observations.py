from datetime import UTC, datetime

from forge.domain import Observation, ObservationSource
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


def _event() -> NormalizedEvent:
    repo = RepositoryRef(
        id="acme/api",
        provider=Provider.GITHUB,
        connection="public",
        namespace="acme",
        default_branch="main",
        change_request_mode="direct",
    )
    return NormalizedEvent(
        id="delivery-7",
        kind=EventKind.CR_UPDATED,
        repo_ref=repo,
        actor=Actor(login="octocat", is_bot=False),
        received_at=datetime(2026, 8, 27, tzinfo=UTC),
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="public", repository_id="acme/api", native_id=42
            ),
            url="https://github.com/acme/api/pull/42",
            title="Change",
            body="Body",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
            head_sha="abc123",
        ),
        raw={"provider": "payload is retained outside the domain contract"},
    )


def test_conversion_is_deterministic_and_json_round_trippable() -> None:
    first = normalized_event_to_observation(_event())
    second = normalized_event_to_observation(_event())

    assert first == second
    assert first.resource.external_id == "acme/api#42"
    assert first.resource_revision == "abc123"
    assert first.facts["kind"] == "cr_updated"
    assert "raw" not in first.facts
    assert Observation.model_validate_json(first.model_dump_json()) == first


def test_poller_and_webhook_use_same_identity_for_same_external_event() -> None:
    webhook = normalized_event_to_observation(_event())
    polled = normalized_event_to_observation(_event(), source=ObservationSource.POLLER)

    assert webhook.observation_id == polled.observation_id
    assert polled.source is ObservationSource.POLLER
