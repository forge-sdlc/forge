"""Control-plane adapter for invoking the typed sandbox station."""

from collections.abc import Mapping
from typing import Any

from forge.domain import StationRequest
from forge.sandbox.runner import ContainerResult, ContainerRunner
from forge.workflow.projections.common import (
    project_invocation_identity,
    project_requested_at,
    project_workflow_identity,
)
from forge.workflow.stations.runner import StationDefinition, invoke_station
from forge.workflow.stations.sandbox_execution import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    SandboxExecutionInput,
    as_container_result,
    run_sandbox_execution_station,
)


async def execute_sandbox_station(
    state: Mapping[str, Any],
    value: SandboxExecutionInput,
    *,
    runner: ContainerRunner,
    discriminator: str,
) -> ContainerResult:
    request = StationRequest[SandboxExecutionInput](
        workflow=project_workflow_identity(state),
        invocation=project_invocation_identity(state, f"{CONTRACT_NAME}:{discriminator}"),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=int(state.get("retry_count") or 0) + 1,
        requested_at=project_requested_at(state),
        input=value,
    )
    async def handler(candidate: StationRequest[Any]):
        return await run_sandbox_execution_station(candidate, runner=runner)

    outcome = await invoke_station(
        StationDefinition(
            CONTRACT_NAME,
            CONTRACT_VERSION,
            SandboxExecutionInput,
            handler,
        ),
        request,
    )
    assert outcome.output is not None
    return as_container_result(outcome.output)


async def execute_sandbox_kwargs(
    state: Mapping[str, Any],
    *,
    runner: ContainerRunner,
    discriminator: str,
    workspace_path: Any,
    task_summary: str,
    task_description: str,
    ticket_key: str = "",
    task_key: str = "",
    repo_name: str = "",
    step_name: str = "",
    policy_key: str = "",
    skill_name: str = "",
    **runner_options: Any,
) -> ContainerResult:
    """Compatibility projection for existing node call sites during cutover."""
    return await execute_sandbox_station(
        state,
        SandboxExecutionInput(
            workspace_path=str(workspace_path),
            task_summary=task_summary,
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=task_key,
            repo_name=repo_name,
            step_name=step_name,
            policy_key=policy_key,
            skill_name=skill_name,
            runner_options=runner_options,
        ),
        runner=runner,
        discriminator=discriminator,
    )
