from datetime import UTC, datetime

from forge.domain import WorkflowCommandType
from forge.models.events import EventSource
from forge.orchestrator.event_adapters import (
    CommandDecisionStatus,
    create_default_event_adapter_registry,
    interpret_event,
)
from forge.queue.models import QueueMessage

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
