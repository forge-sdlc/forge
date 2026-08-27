"""Project checkpoints into the approval-policy station contract."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationRequest
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.approval import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ApprovalInput,
)


def project_approval(
    state: Mapping[str, Any],
    stage: str,
    *,
    item_count: int | None = None,
) -> StationRequest[ApprovalInput]:
    context = state.get("context") if isinstance(state.get("context"), Mapping) else {}
    current_task = state.get("current_task_key") or context.get("rejected_task_key")
    current_epic = state.get("current_epic_key") or context.get("rejected_epic_key")
    return StationRequest[ApprovalInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{stage}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=ApprovalInput(
            stage=stage,
            paused=bool(state.get("is_paused")),
            yolo_mode=bool(state.get("yolo_mode")),
            is_question=bool(state.get("is_question")),
            revision_requested=bool(state.get("revision_requested")),
            feedback=state.get("feedback_comment"),
            item_count=item_count,
            current_item=current_task or current_epic,
            revision_scope=("task" if current_task else "epic" if current_epic else "all"),
        ),
    )
