"""Round-trip serialization tests for NormalizedEvent <-> QueueMessage."""

from datetime import UTC, datetime

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    Review,
    ReviewComment,
    ReviewState,
)
from forge.models.events import EventSource
from forge.queue.models import QueueMessage, normalized_event_from_dict, normalized_event_to_dict


def _sample_event() -> NormalizedEvent:
    repo_ref = RepositoryRef(
        id="test/repo",
        provider=Provider.GITHUB,
        connection="default-github",
        namespace="test/repo",
        default_branch="main",
        change_request_mode="fork",
    )
    change_request = ChangeRequest(
        identity=ChangeRequestIdentity(
            connection="default-github", repository_id="test/repo", native_id=42
        ),
        url="https://github.com/test/repo/pull/42",
        title="Test PR",
        body="body",
        state=ChangeRequestState.OPEN,
        source_branch="feature",
        target_branch="main",
        head_sha="sha123",
        draft=False,
    )
    return NormalizedEvent(
        id="delivery-123",
        kind=EventKind.CR_OPENED,
        repo_ref=repo_ref,
        actor=Actor(login="octocat", is_bot=False),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        change_request=change_request,
        raw={"action": "opened"},
    )


def test_normalized_event_round_trips_through_dict():
    event = _sample_event()

    data = normalized_event_to_dict(event)
    restored = normalized_event_from_dict(data)

    assert restored.id == event.id
    assert restored.kind == event.kind
    assert restored.repo_ref == event.repo_ref
    assert restored.actor == event.actor
    assert restored.received_at == event.received_at
    assert restored.change_request == event.change_request
    assert restored.change_request.head_sha == "sha123"
    assert restored.comment is None
    assert restored.review is None
    assert restored.check is None
    assert restored.raw == event.raw


def test_queue_message_from_redis_maps_legacy_github_source():
    """Retry/DLQ entries and unconsumed stream messages persisted before the
    EventSource "github" -> "source_control" rename must still decode."""
    message = QueueMessage.from_redis(
        "1-0",
        {
            "event_id": "evt-1",
            "source": "github",
            "event_type": "cr_opened",
            "ticket_key": "PROJ-1",
            "payload": "{}",
            "normalized_event": "",
            "timestamp": "2026-01-01T00:00:00",
            "retry_count": "0",
        },
    )

    assert message.source == EventSource.SOURCE_CONTROL


def test_normalized_event_round_trips_with_no_change_request():
    event = _sample_event()
    event.change_request = None

    data = normalized_event_to_dict(event)
    restored = normalized_event_from_dict(data)

    assert restored.change_request is None


def test_normalized_event_round_trips_populated_comment():
    event = _sample_event()
    event.kind = EventKind.COMMENT_CREATED
    event.comment = ReviewComment(
        id="c1",
        body="/forge skip-gate flaky-test",
        author="octocat",
        path="src/app.py",
        line=12,
        resolved=True,
        in_reply_to="c0",
    )

    data = normalized_event_to_dict(event)
    restored = normalized_event_from_dict(data)

    assert restored.comment == event.comment


def test_normalized_event_round_trips_populated_review():
    event = _sample_event()
    event.kind = EventKind.REVIEW_SUBMITTED
    event.review = Review(
        id="r1",
        state=ReviewState.CHANGES_REQUESTED,
        body="please fix X",
        author="reviewer1",
        comments=[
            ReviewComment(
                id="rc1",
                body="inline nit",
                author="reviewer1",
                path="src/app.py",
                line=7,
            )
        ],
    )

    data = normalized_event_to_dict(event)
    restored = normalized_event_from_dict(data)

    assert restored.review == event.review


def test_normalized_event_round_trips_check_output():
    event = _sample_event()
    event.kind = EventKind.CHECK_UPDATED
    event.check = CheckRun(
        name="build",
        status=CheckStatus.COMPLETED,
        conclusion=CheckConclusion.FAILURE,
        url="https://github.com/test/repo/runs/1",
        logs_url="https://github.com/test/repo/runs/1/logs",
        output={"title": "Build failed", "summary": "2 tests failed", "text": "details"},
    )

    data = normalized_event_to_dict(event)
    restored = normalized_event_from_dict(data)

    assert restored.check == event.check
    assert restored.check.output == event.check.output
