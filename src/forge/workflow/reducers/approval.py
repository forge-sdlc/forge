"""Allowlisted checkpoint updates for approval-policy outcomes."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationOutcome, StationRequest
from forge.workflow.reducers.common import validate_station_outcome
from forge.workflow.stations.approval import (
    ApprovalDisposition,
    ApprovalInput,
    ApprovalOutput,
)


def reduce_approval_gate(
    state: Mapping[str, Any],
    request: StationRequest[ApprovalInput],
    outcome: StationOutcome[ApprovalOutput],
    gate_name: str,
    retry_node: str,
) -> dict[str, Any]:
    validate_station_outcome(state, request, outcome)
    assert outcome.output is not None
    if outcome.output.disposition is ApprovalDisposition.INVALID:
        return {
            "last_error": outcome.output.reason,
            "current_node": retry_node,
            "retry_count": int(state.get("retry_count") or 0) + 1,
            "is_paused": False,
        }
    return {"is_paused": True, "current_node": gate_name}
