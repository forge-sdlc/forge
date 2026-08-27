from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.domain import StationInvocationIdentity, StationRequest, WorkflowIdentity
from forge.workflow.stations.agent_operation import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    AgentOperation,
    AgentOperationInput,
    run_agent_operation_station,
)


def _request(value: AgentOperationInput) -> StationRequest[AgentOperationInput]:
    return StationRequest[AgentOperationInput](
        workflow=WorkflowIdentity(
            run_id="FORGE-1", workflow_name="feature", definition_revision=1
        ),
        invocation=StationInvocationIdentity(
            invocation_id="FORGE-1:agent", station_name=CONTRACT_NAME
        ),
        contract_name=CONTRACT_NAME,
        contract_version=CONTRACT_VERSION,
        attempt=1,
        requested_at=datetime.now(UTC),
        input=value,
    )


@pytest.mark.asyncio
async def test_run_task_strips_transport_preamble() -> None:
    agent = MagicMock()
    agent.run_task = AsyncMock(return_value="raw")
    agent._strip_preamble.return_value = "plan"
    agent.close = AsyncMock()
    with patch("forge.workflow.stations.agent_operation.ForgeAgent", return_value=agent):
        outcome = await run_agent_operation_station(
            _request(
                AgentOperationInput(
                    operation=AgentOperation.RUN_TASK,
                    task="planning",
                    policy_key="planning",
                    prompt="make plan",
                )
            )
        )

    assert outcome.output is not None
    assert outcome.output.text == "plan"
    agent.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_question_has_a_typed_contract() -> None:
    agent = AsyncMock()
    agent.answer_question.return_value = "Because the gate is pending."
    with patch("forge.workflow.stations.agent_operation.ForgeAgent", return_value=agent):
        outcome = await run_agent_operation_station(
            _request(
                AgentOperationInput(
                    operation=AgentOperation.ANSWER_QUESTION,
                    question="Why?",
                    artifact_content="Plan",
                )
            )
        )

    assert outcome.output is not None
    assert outcome.output.text == "Because the gate is pending."
