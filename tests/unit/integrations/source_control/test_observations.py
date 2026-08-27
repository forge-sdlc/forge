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
    ReviewComment,
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


def test_poller_and_webhook_deduplicate_revision_even_with_different_delivery_ids() -> None:
    webhook_event = _event()
    poller_event = _event()
    poller_event.id = "poller-observation-99"

    webhook = normalized_event_to_observation(webhook_event)
    polled = normalized_event_to_observation(poller_event, source=ObservationSource.POLLER)

    # The observation records retain their provider delivery identity, while
    # the delivery key is derived from the external revision and is shared.
    assert webhook.observation_id != polled.observation_id
    assert webhook.delivery_identity == polled.delivery_identity


def test_event_resources_do_not_share_a_change_request_delivery_key() -> None:
    first = _event()
    first.kind = EventKind.COMMENT_CREATED
    first.comment = ReviewComment(id="comment-1", body="one", author="octocat")
    second = _event()
    second.kind = EventKind.COMMENT_CREATED
    second.comment = ReviewComment(id="comment-2", body="two", author="octocat")

    first_observation = normalized_event_to_observation(first)
    second_observation = normalized_event_to_observation(second)

    assert first_observation.resource.resource_type == "comment"
    assert first_observation.delivery_identity != second_observation.delivery_identity
