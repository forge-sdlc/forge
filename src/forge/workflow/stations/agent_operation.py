"""Typed station boundary for bounded text-agent operations."""

from __future__ import annotations

import inspect
from enum import StrEnum

from pydantic import Field

from forge.domain import (
    DomainModel,
    JsonValue,
    StationOutcome,
    StationOutcomeStatus,
    StationRequest,
)
from forge.integrations.agents import ForgeAgent

CONTRACT_NAME = "agent-operation"
CONTRACT_VERSION = "1.0"


class AgentOperation(StrEnum):
    RUN_TASK = "run_task"
    ANSWER_QUESTION = "answer_question"


class AgentOperationInput(DomainModel):
    operation: AgentOperation
    task: str | None = None
    policy_key: str | None = None
    prompt: str | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)
    trace_context: dict[str, JsonValue] = Field(default_factory=dict)
    include_tools: bool = True
    question: str | None = None
    artifact_content: str | None = None


class AgentOperationOutput(DomainModel):
    text: str


async def run_agent_operation_station(
    request: StationRequest[AgentOperationInput],
) -> StationOutcome[AgentOperationOutput]:
    value = request.input
    agent = ForgeAgent()
    try:
        if value.operation is AgentOperation.RUN_TASK:
            if not value.task or not value.policy_key or value.prompt is None:
                raise ValueError("run_task requires task, policy_key, and prompt")
            text = await agent.run_task(
                task=value.task,
                policy_key=value.policy_key,
                prompt=value.prompt,
                context=dict(value.context),
                trace_context=dict(value.trace_context),
                include_tools=value.include_tools,
            )
            stripped = agent._strip_preamble(text)
            text = (stripped if isinstance(stripped, str) else text).strip()
        else:
            if value.question is None or value.artifact_content is None:
                raise ValueError("answer_question requires question and artifact_content")
            text = await agent.answer_question(
                question=value.question,
                artifact_content=value.artifact_content,
                context=dict(value.context),
            )
    finally:
        close_result = agent.close()
        if inspect.isawaitable(close_result):
            await close_result
    if not text.strip():
        raise ValueError("Agent operation returned empty output")
    return StationOutcome[AgentOperationOutput](
        workflow=request.workflow,
        invocation=request.invocation,
        contract_name=request.contract_name,
        contract_version=request.contract_version,
        status=StationOutcomeStatus.SUCCEEDED,
        completed_at=request.requested_at,
        output=AgentOperationOutput(text=text),
    )
