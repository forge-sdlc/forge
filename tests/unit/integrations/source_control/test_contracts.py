"""Tests for source control contract data models."""

from datetime import UTC, datetime

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequestIdentity,
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


def _repo_ref() -> RepositoryRef:
    return RepositoryRef(
        id="payments-api",
        provider=Provider.GITHUB,
        connection="public-github",
        namespace="acme/payments",
        default_branch="main",
        change_request_mode="fork",
    )


def test_repository_ref_holds_identity_and_mode():
    ref = _repo_ref()
    assert ref.id == "payments-api"
    assert ref.provider is Provider.GITHUB
    assert ref.change_request_mode == "fork"


def test_change_request_identity_equality_is_by_value():
    a = ChangeRequestIdentity(
        connection="public-github", repository_id="payments-api", native_id=42
    )
    b = ChangeRequestIdentity(
        connection="public-github", repository_id="payments-api", native_id=42
    )
    c = ChangeRequestIdentity(
        connection="public-github", repository_id="payments-api", native_id=None
    )
    assert a == b
    assert a != c


def test_change_request_identity_native_id_defaults_to_none():
    identity = ChangeRequestIdentity(connection="public-github", repository_id="payments-api")
    assert identity.native_id is None


def test_normalized_event_only_populates_the_relevant_payload():
    event = NormalizedEvent(
        id="evt-1",
        kind=EventKind.CHECK_UPDATED,
        repo_ref=_repo_ref(),
        actor=Actor(login="forge-bot", is_bot=True),
        received_at=datetime(2026, 8, 18, tzinfo=UTC),
        check=CheckRun(
            name="CI / Tests", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS
        ),
    )
    assert event.kind is EventKind.CHECK_UPDATED
    assert event.check is not None
    assert event.check.conclusion is CheckConclusion.SUCCESS
    assert event.change_request is None
    assert event.review is None
    assert event.comment is None


def test_review_carries_its_comments():
    review = Review(
        id="rev-1",
        state=ReviewState.CHANGES_REQUESTED,
        body="Please fix the null check",
        author="reviewer1",
        comments=[
            ReviewComment(
                id="c1", body="null check missing", author="reviewer1", path="a.py", line=10
            )
        ],
    )
    assert review.state is ReviewState.CHANGES_REQUESTED
    assert len(review.comments) == 1
    assert review.comments[0].path == "a.py"
