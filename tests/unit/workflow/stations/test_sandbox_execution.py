from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.sandbox.runner import ContainerResult
from forge.workflow.stations.sandbox_execution import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    SandboxExecutionInput,
    as_container_result,
    run_sandbox_execution_station,
)


def _request() -> StationRequest[SandboxExecutionInput]:
    return StationRequest[SandboxExecutionInput](
        workflow=WorkflowIdentity(
            run_id="FORGE-1", workflow_name="feature", definition_revision=1
        ),
        invocation=StationInvocationIdentity(
            invocation_id="FORGE-1:execute", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=datetime.now(UTC),
        input=SandboxExecutionInput(
            workspace_path="/tmp/work",
            task_summary="Implement",
            task_description="Do work",
            ticket_key="FORGE-1",
            task_key="FORGE-2",
            repo_name="org/repo",
            step_name="implement",
            policy_key="implement_task",
            skill_name="implement-task",
        ),
    )


@pytest.mark.asyncio
async def test_sandbox_execution_is_invoked_from_typed_input() -> None:
    runner = AsyncMock()
    runner.run.return_value = ContainerResult(
        success=True, exit_code=0, stdout="done", stderr=""
    )

    outcome = await run_sandbox_execution_station(_request(), runner=runner)

    assert outcome.output is not None
    assert outcome.output.success is True
    assert as_container_result(outcome.output).stdout == "done"
    assert runner.run.await_args.kwargs["ticket_key"] == "FORGE-1"
