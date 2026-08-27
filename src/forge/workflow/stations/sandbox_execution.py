"""Typed station for one independently runnable sandbox execution."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from pydantic import Field

from forge.domain import (
    DomainModel,
    JsonValue,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
)
from forge.sandbox.runner import ContainerResult, ContainerRunner

CONTRACT_NAME = "sandbox-execution"
CONTRACT_VERSION = "1.0"


class SandboxExecutionInput(DomainModel):
    workspace_path: str
    task_summary: str
    task_description: str
    ticket_key: str
    task_key: str
    repo_name: str
    step_name: str
    policy_key: str
    skill_name: str
    runner_options: dict[str, JsonValue] = Field(default_factory=dict)


class SandboxExecutionOutput(DomainModel):
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    tests_passed: bool | None = None
    error_message: str | None = None
    review_cycles: tuple[dict[str, JsonValue], ...] = ()


async def run_sandbox_execution_station(
    request: StationRequest[SandboxExecutionInput],
    *,
    runner: ContainerRunner | None = None,
) -> StationOutcome[SandboxExecutionOutput]:
    value = request.input
    runtime = runner or ContainerRunner()
    result = await runtime.run(
        workspace_path=Path(value.workspace_path),
        task_summary=value.task_summary,
        task_description=value.task_description,
        ticket_key=value.ticket_key,
        task_key=value.task_key,
        repo_name=value.repo_name,
        step_name=value.step_name,
        policy_key=value.policy_key,
        skill_name=value.skill_name,
        **value.runner_options,
    )
    output = SandboxExecutionOutput(
        success=bool(result.success),
        exit_code=int(result.exit_code),
        stdout=result.stdout if isinstance(result.stdout, str) else str(result.stdout),
        stderr=result.stderr if isinstance(result.stderr, str) else str(result.stderr),
        tests_passed=result.tests_passed if isinstance(result.tests_passed, bool) else None,
        error_message=result.error_message if isinstance(result.error_message, str) else None,
        review_cycles=tuple(
            asdict(cycle) for cycle in result.review_cycles if hasattr(cycle, "cycle")
        ),
    )
    return StationOutcome[SandboxExecutionOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=(
            StationOutcomeStatus.SUCCEEDED
            if result.success
            else StationOutcomeStatus.RETRYABLE_FAILURE
        ),
        completed_at=request.requested_at,
        output=output,
        reason=output.error_message,
    )


def as_container_result(output: SandboxExecutionOutput) -> ContainerResult:
    """Adapt typed station output for legacy checkpoint projection during cutover."""
    from forge.observability import ReviewCycleData

    return ContainerResult(
        success=output.success,
        exit_code=output.exit_code,
        stdout=output.stdout,
        stderr=output.stderr,
        tests_passed=output.tests_passed,
        error_message=output.error_message,
        review_cycles=[ReviewCycleData.from_dict(dict(cycle)) for cycle in output.review_cycles],
    )
