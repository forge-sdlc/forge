from datetime import UTC, datetime

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.stations.approval import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ApprovalDisposition,
    ApprovalInput,
    run_approval_station,
)


def request(**values) -> StationRequest[ApprovalInput]:
    return StationRequest[ApprovalInput](
        workflow=WorkflowIdentity(run_id="run", workflow_name="feature", definition_revision=1),
        invocation=StationInvocationIdentity(invocation_id="inv", station_name=CONTRACT_NAME),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=datetime.now(UTC),
        input=ApprovalInput(stage="prd", **values),
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"is_question": True, "feedback": "why?"}, ApprovalDisposition.QUESTION),
        ({"yolo_mode": True}, ApprovalDisposition.APPROVED),
        (
            {"revision_requested": True, "feedback": "change it"},
            ApprovalDisposition.REVISION,
        ),
        ({"paused": True}, ApprovalDisposition.WAITING),
        ({}, ApprovalDisposition.APPROVED),
        ({"item_count": 0}, ApprovalDisposition.INVALID),
    ],
)
def test_approval_policy_is_provider_and_graph_independent(values, expected) -> None:
    outcome = run_approval_station(request(**values))

    assert outcome.output is not None
    assert outcome.output.disposition is expected
