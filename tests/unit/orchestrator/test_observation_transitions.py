"""Contract tests for the workflow-boundary observation transition reducer.

These tests deliberately call the transition boundary rather than
``OrchestratorWorker``.  Provider observations are normalized before they reach
this API; the worker is only responsible for dispatching the call and
persisting its result.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckStatus,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
    Review,
    ReviewState,
)
from forge.models.events import EventSource
from forge.orchestrator.event_adapters import (
    CommandDecision,
    CommandDecisionStatus,
    create_default_event_adapter_registry,
)
from forge.queue.models import QueueMessage, normalized_event_to_dict
from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.transitions import (
    ObservationTransitionPolicy,
    apply_observation_transition,
)


def _policy() -> ObservationTransitionPolicy:
    definition = builtin_feature_definition()
    return ObservationTransitionPolicy(
        identifier="post-pr-v1", definition=definition.canonical_dict()
    )

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _repo(name: str = "acme/payments") -> RepositoryRef:
    return RepositoryRef(
        id=name,
        provider=Provider.GITHUB,
        connection="default-github",
        namespace=name,
        default_branch="main",
        change_request_mode="fork",
    )


def _change_request(
    repo: RepositoryRef,
    number: int = 42,
    state: ChangeRequestState = ChangeRequestState.OPEN,
) -> ChangeRequest:
    return ChangeRequest(
        identity=ChangeRequestIdentity(
            connection=repo.connection,
            repository_id=repo.id,
            native_id=number,
        ),
        url=f"https://github.com/{repo.namespace}/pull/{number}",
        title="Change",
        body="",
        state=state,
        source_branch="feature",
        target_branch="main",
        draft=False,
    )


def _event(
    kind: EventKind,
    *,
    repo: RepositoryRef | None = None,
    change_request: ChangeRequest | None = None,
    check_suite_status: CheckStatus | None = None,
    review: Review | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        id="provider-event-1",
        kind=kind,
        repo_ref=repo or _repo(),
        actor=Actor(login="reviewer", is_bot=False),
        received_at=NOW,
        change_request=change_request,
        check_suite_status=check_suite_status,
        review=review,
        raw={},
    )


def _message(event: NormalizedEvent, *, ticket_key: str = "FORGE-42") -> QueueMessage:
    return QueueMessage(
        message_id="message-1",
        event_id=event.id,
        source=EventSource.SOURCE_CONTROL,
        event_type=event.kind.value,
        ticket_key=ticket_key,
        payload={},
        normalized_event=normalized_event_to_dict(event),
        timestamp=NOW,
    )


def _adapted(message: QueueMessage):
    return create_default_event_adapter_registry().adapt(message)


def _ignored_decision() -> CommandDecision:
    return CommandDecision(CommandDecisionStatus.IGNORED, "test observation")


def _runtime(_event: NormalizedEvent) -> MagicMock:
    runtime = MagicMock()
    runtime._event_adapter_registry.return_value = (
        create_default_event_adapter_registry()
    )
    runtime._get_forge_github_login = AsyncMock(return_value="forge-bot")
    return runtime


@pytest.mark.asyncio
async def test_completed_ci_observation_is_applied_at_the_boundary() -> None:
    repo = _repo()
    event = _event(
        EventKind.CHECK_UPDATED,
        repo=repo,
        change_request=_change_request(repo),
        check_suite_status=CheckStatus.COMPLETED,
    )
    message = _message(event)
    state = {
        "ticket_key": "FORGE-42",
        "current_node": "ci_evaluator",
        "is_paused": True,
        "context": {},
    }

    result = await apply_observation_transition(
        _runtime(event),
        message,
        state,
        adapted_event=_adapted(message),
        command_decision=_ignored_decision(),
        policy=_policy(),
    )

    assert result is not state
    assert result["is_paused"] is False


@pytest.mark.asyncio
async def test_incomplete_ci_observation_is_a_noop_at_the_boundary() -> None:
    repo = _repo()
    event = _event(
        EventKind.CHECK_UPDATED,
        repo=repo,
        change_request=_change_request(repo),
        check_suite_status=CheckStatus.IN_PROGRESS,
    )
    message = _message(event)
    state = {
        "ticket_key": "FORGE-42",
        "current_node": "ci_evaluator",
        "is_paused": True,
        "context": {},
    }

    result = await apply_observation_transition(
        _runtime(event),
        message,
        state,
        adapted_event=_adapted(message),
        command_decision=_ignored_decision(),
        policy=_policy(),
    )

    assert result is state


@pytest.mark.asyncio
async def test_review_approval_is_applied_without_worker_event_interpretation() -> None:
    repo = _repo()
    event = _event(
        EventKind.REVIEW_SUBMITTED,
        repo=repo,
        change_request=_change_request(repo),
        review=Review(
            id="review-1",
            state=ReviewState.APPROVED,
            body="Looks good",
            author="reviewer",
        ),
    )
    message = _message(event)
    state = {
        "ticket_key": "FORGE-42",
        "current_node": "human_review_gate",
        "is_paused": True,
        "current_repo": repo.namespace,
        "current_pr_number": 42,
        "pull_requests": {
            f"{repo.namespace}:42": {
                "repo": repo.namespace,
                "number": 42,
                "merged": False,
            }
        },
        "context": {},
    }
    runtime = _runtime(event)
    runtime._get_forge_github_login.return_value = "forge-bot"

    result = await apply_observation_transition(
        runtime,
        message,
        state,
        adapted_event=_adapted(message),
        command_decision=_ignored_decision(),
        policy=_policy(),
    )

    assert result["human_review_status"] == "approved"
    assert result["is_paused"] is True


@pytest.mark.asyncio
async def test_merge_for_an_untracked_pull_request_is_ignored() -> None:
    repo = _repo()
    event = _event(
        EventKind.CR_MERGED,
        repo=repo,
        change_request=_change_request(repo, number=43, state=ChangeRequestState.MERGED),
    )
    message = _message(event)
    state = {
        "ticket_key": "FORGE-42",
        "current_node": "human_review_gate",
        "is_paused": True,
        "current_repo": repo.namespace,
        "current_pr_number": 42,
        "pull_requests": {
            f"{repo.namespace}:42": {
                "repo": repo.namespace,
                "number": 42,
                "merged": False,
            }
        },
        "context": {},
    }

    result = await apply_observation_transition(
        _runtime(event),
        message,
        state,
        adapted_event=_adapted(message),
        command_decision=_ignored_decision(),
        policy=_policy(),
    )

    assert result is state
