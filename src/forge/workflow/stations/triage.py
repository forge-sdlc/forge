"""Provider-independent ticket completeness evaluation station."""

from __future__ import annotations

from enum import StrEnum

from forge.domain import DomainModel, StationOutcome, StationOutcomeStatus, StationRequest
from forge.integrations.agents import ForgeAgent
from forge.prompts import load_prompt

CONTRACT_NAME = "triage-evaluation"
CONTRACT_VERSION = "1.0"


class TriageKind(StrEnum):
    BUG = "bug"
    TASK_TAKEOVER = "task_takeover"


class TriageInput(DomainModel):
    kind: TriageKind
    ticket_key: str
    summary: str = ""
    description: str = ""
    comments: str = ""


class TriageOutput(DomainModel):
    sufficient: bool
    missing_fields: tuple[str, ...] = ()


async def run_triage_station(
    request: StationRequest[TriageInput],
) -> StationOutcome[TriageOutput]:
    value = request.input
    prompt_name = "triage-bug" if value.kind is TriageKind.BUG else "task-takeover-triage"
    task_name = prompt_name
    policy_key = "bug_triage" if value.kind is TriageKind.BUG else "task_takeover_triage"
    agent = ForgeAgent()
    try:
        output = await agent.run_structured_task(
            task=task_name,
            policy_key=policy_key,
            response_schema=TriageOutput,
            prompt=load_prompt(
                prompt_name,
                summary=value.summary,
                description=value.description,
                comments=value.comments,
            ),
            context={"ticket_key": value.ticket_key},
        )
    finally:
        await agent.close()

    return StationOutcome[TriageOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=output,
    )
