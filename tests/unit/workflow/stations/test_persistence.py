from datetime import UTC, datetime

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.stations.persistence import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    PersistenceAction,
    PersistenceInput,
    run_persistence_station,
)


def test_persistence_station_emits_stable_effect_intents() -> None:
    request = StationRequest[PersistenceInput](
        workflow=WorkflowIdentity(
            run_id="FORGE-1", workflow_name="feature", definition_revision=1
        ),
        invocation=StationInvocationIdentity(
            invocation_id="FORGE-1:persist", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=datetime.now(UTC),
        input=PersistenceInput(
            actions=(
                PersistenceAction(
                    operation="jira.issue.transition",
                    resource_type="issue",
                    external_id="FORGE-2",
                    logical_action="complete-task",
                    payload={"transition": "Closed"},
                ),
            )
        ),
    )

    first = run_persistence_station(request)
    second = run_persistence_station(request.model_copy(update={"attempt": 2}))

    assert first.requested_effects[0].effect_id == second.requested_effects[0].effect_id
    assert first.output is not None
    assert first.output.effect_ids == (first.requested_effects[0].effect_id,)
