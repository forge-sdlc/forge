from datetime import UTC, datetime

from forge.domain import WorkflowCommandType
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
from forge.models.events import EventSource
from forge.orchestrator.event_adapters import (
    CommandDecisionStatus,
    create_default_event_adapter_registry,
    interpret_event,
)
from forge.queue.models import QueueMessage, normalized_event_to_dict

NOW = datetime(2026, 8, 27, tzinfo=UTC)
STATE = {
    "thread_id": "FORGE-42",
    "ticket_key": "FORGE-42",
    "workflow_name": "feature",
    "workflow_definition_revision": 3,
    "current_node": "spec_approval_gate",
}


def _message(payload: dict) -> QueueMessage:
    return QueueMessage(
        message_id="1",
        event_id="jira-1",
        source=EventSource.JIRA,
        event_type="issue_updated",
        ticket_key="FORGE-42",
        payload={
            "issue": {
                "key": "FORGE-42",
                "fields": {"issuetype": {"name": "Feature"}},
            },
            **payload,
        },
        timestamp=NOW,
    )


def _interpret(message: QueueMessage):
    adapted = create_default_event_adapter_registry().adapt(message)
    return interpret_event(message, adapted, STATE)


def test_matching_approval_becomes_versioned_command() -> None:
    decision = _interpret(
        _message(
            {
                "changelog": {
                    "items": [
                        {
                            "field": "labels",
                            "fromString": "forge:spec-pending",
                            "toString": "forge:spec-approved",
                        }
                    ]
                }
            }
        )
    )

    assert decision.status is CommandDecisionStatus.ACCEPTED
    assert decision.command is not None
    assert decision.command.command_type is WorkflowCommandType.APPROVE
    assert decision.command.workflow.definition_revision == 3
    assert decision.command.observation_ids


def test_approval_for_wrong_stage_is_inspectably_ignored() -> None:
    decision = _interpret(
        _message(
            {
                "changelog": {
                    "items": [
                        {
                            "field": "labels",
                            "fromString": "forge:prd-pending",
                            "toString": "forge:prd-approved",
                        }
                    ]
                }
            }
        )
    )

    assert decision.status is CommandDecisionStatus.IGNORED
    assert decision.reason == "no eligible workflow signal"


def test_retry_identity_is_stable_for_duplicate_delivery() -> None:
    message = _message(
        {"changelog": {"items": [{"field": "labels", "fromString": "", "toString": "forge:retry"}]}}
    )

    first = _interpret(message).command
    second = _interpret(message).command

    assert first is not None
    assert first == second
    assert first.command_type is WorkflowCommandType.RETRY


def test_revision_comment_becomes_reject_command() -> None:
    decision = _interpret(_message({"comment": {"body": "! Please add failure handling"}}))

    assert decision.command is not None
    assert decision.command.command_type is WorkflowCommandType.REJECT
    assert decision.command.arguments["feedback"] == "Please add failure handling"


def test_yolo_label_becomes_explicit_command() -> None:
    decision = _interpret(
        _message(
            {
                "changelog": {
                    "items": [
                        {
                            "field": "labels",
                            "fromString": "forge:spec-pending",
                            "toString": "forge:spec-pending forge:yolo",
                        }
                    ]
                }
            }
        )
    )

    assert decision.command is not None
    assert decision.command.command_type is WorkflowCommandType.ENABLE_YOLO


def test_rca_option_becomes_explicit_command() -> None:
    state = {**STATE, "current_node": "rca_option_gate", "rca_options": ["one", "two"]}
    message = _message({"comment": {"body": ">option 2"}})
    adapted = create_default_event_adapter_registry().adapt(message)

    decision = interpret_event(message, adapted, state)

    assert decision.command is not None
    assert decision.command.command_type is WorkflowCommandType.SELECT_OPTION
    assert decision.command.arguments["option"] == 2


def test_source_control_control_comment_becomes_explicit_command() -> None:
    repo = RepositoryRef(
        id="1",
        provider=Provider.GITHUB,
        connection="default",
        namespace="acme/repo",
        default_branch="main",
        change_request_mode="direct",
    )
    event = NormalizedEvent(
        id="github-1",
        kind=EventKind.COMMENT_CREATED,
        repo_ref=repo,
        actor=Actor(login="alice", is_bot=False),
        received_at=NOW,
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="default", repository_id="1", native_id="7"
            ),
            url="https://example.test/acme/repo/pull/7",
            title="PR",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
        ),
        comment=ReviewComment(id="2", body="/forge skip-gate lint", author="alice"),
    )
    message = QueueMessage(
        message_id="1",
        event_id="github-1",
        source=EventSource.SOURCE_CONTROL,
        event_type="issue_comment",
        ticket_key="FORGE-42",
        payload={},
        normalized_event=normalized_event_to_dict(event),
        timestamp=NOW,
    )
    state = {**STATE, "current_node": "ci_evaluator", "current_pr_number": 7}
    adapted = create_default_event_adapter_registry().adapt(message)

    decision = interpret_event(message, adapted, state)

    assert decision.command is not None
    assert decision.command.command_type is WorkflowCommandType.SKIP_GATE
    assert decision.command.arguments["check_name"] == "lint"
