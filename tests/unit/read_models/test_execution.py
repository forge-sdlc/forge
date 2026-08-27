from datetime import UTC, datetime, timedelta

from forge.domain import (
    EffectCommand,
    EffectResult,
    EffectResultStatus,
    Observation,
    ObservationSource,
    ResourceIdentity,
    WorkflowIdentity,
)
from forge.effects import EffectRecord, EffectRecordStatus
from forge.read_models.execution import project_execution
from forge.read_models.models import ExecutionStatus
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import build_process_manifest

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _manifest():
    definition = load_workflow_value(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "feature-flow", "revision": 2},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {
                    "generate_prd": {"next": "prd_approval_gate"},
                    "prd_approval_gate": {
                        "route": "route_prd_approval",
                        "branches": {
                            "approved": "__end__",
                            "revise": "generate_prd",
                        },
                    },
                },
            },
        }
    )
    return build_process_manifest(definition)


def _effect() -> EffectRecord:
    workflow = WorkflowIdentity(
        run_id="FORGE-1", workflow_name="feature-flow", definition_revision=2
    )
    command = EffectCommand(
        effect_id="effect-1",
        idempotency_key="effect-1",
        workflow=workflow,
        operation="jira.comment.create",
        target=ResourceIdentity(resource_type="issue", external_id="FORGE-1"),
    )
    result = EffectResult(
        effect_id="effect-1",
        idempotency_key="effect-1",
        status=EffectResultStatus.SUCCEEDED,
        completed_at=NOW,
        provider_reference="comment-7",
    )
    return EffectRecord(
        command=command,
        status=EffectRecordStatus.SUCCEEDED,
        attempt=1,
        created_at=NOW,
        updated_at=NOW,
        next_attempt_at=NOW,
        result=result,
    )


def test_waiting_instance_explains_position_commands_and_next_transitions() -> None:
    checkpoint = {
        "thread_id": "FORGE-1",
        "ticket_key": "FORGE-1",
        "workflow_name": "feature-flow",
        "workflow_revision": 2,
        "workflow_digest": _manifest().digest,
        "current_node": "prd_approval_gate",
        "is_paused": True,
        "updated_at": NOW.isoformat(),
    }

    model = project_execution(checkpoint, effects=[_effect()], manifest=_manifest(), now=NOW)

    assert model.status is ExecutionStatus.WAITING
    assert model.waiting is not None
    assert model.waiting.code == "gate"
    assert model.permitted_commands == ("approve", "reject", "resume", "retry", "cancel")
    assert {(item.outcome, item.target) for item in model.next_transitions} == {
        ("approved", "__end__"),
        ("revise", "generate_prd"),
    }
    assert model.effects[0].provider_reference == "comment-7"


def test_blocked_instance_has_recovery_without_logs() -> None:
    model = project_execution(
        {
            "ticket_key": "FORGE-1",
            "current_node": "implement_work",
            "is_blocked": True,
            "last_error": "Required repository credential is unavailable",
        }
    )

    assert model.status is ExecutionStatus.BLOCKED
    assert model.waiting is not None
    assert model.waiting.message == "Required repository credential is unavailable"
    assert model.permitted_commands == ("retry", "cancel")
    assert model.definition.available is False


def test_observation_staleness_is_explicit() -> None:
    observation = Observation(
        observation_id="observation-1",
        source=ObservationSource.POLLER,
        source_system="github",
        resource=ResourceIdentity(resource_type="change_request", external_id="repo#1"),
        observed_at=NOW - timedelta(hours=2),
        received_at=NOW - timedelta(hours=2),
    )

    model = project_execution(
        {"ticket_key": "FORGE-1", "current_node": "ci_evaluator", "is_paused": True},
        last_observation=observation,
        now=NOW,
    )

    assert model.last_observation.available is True
    assert model.last_observation.stale is True


def test_station_history_is_projected_without_complete_checkpoint_state() -> None:
    model = project_execution(
        {
            "ticket_key": "FORGE-1",
            "current_node": "setup_workspace",
            "station_history": [
                {
                    "station_name": "task-routing",
                    "invocation_id": "invocation-1",
                    "attempt": 1,
                    "status": "succeeded",
                    "completed_at": NOW.isoformat(),
                }
            ],
        }
    )

    assert model.station_attempts[0].station_name == "task-routing"
    assert model.station_attempts[0].status == "succeeded"
