"""Tests for generic workflow-node preconditions."""

import pytest

from forge.workflow.preconditions import (
    CapabilityName,
    NodeContract,
    PreconditionAction,
    Requirement,
    evaluate_preconditions,
    has_capability,
    project_capabilities,
    with_preconditions,
)


def test_explicit_capability_overrides_compatibility_inference() -> None:
    state = {
        "workspace_path": "/tmp/workspace",
        "capabilities": {CapabilityName.WORKSPACE.value: False},
    }

    assert not has_capability(state, CapabilityName.WORKSPACE)


def test_capabilities_must_be_projected_before_evaluation() -> None:
    repository_state = {"current_repo": "owner/repo"}
    pull_request_state = {"pr_urls": ["https://example.test/pr/1"]}

    assert not has_capability(repository_state, CapabilityName.REPOSITORIES)
    assert has_capability(
        {**repository_state, "capabilities": project_capabilities(repository_state)},
        CapabilityName.REPOSITORIES,
    )
    assert has_capability(
        {**pull_request_state, "capabilities": project_capabilities(pull_request_state)},
        CapabilityName.PULL_REQUEST,
    )


def test_repository_capability_is_explicitly_projected_from_jira_event_labels() -> None:
    state = {
        "context": {
            "payload": {
                "issue": {
                    "fields": {
                        "labels": ["forge:managed", "repo:forge-sdlc/forge"],
                    }
                }
            }
        }
    }

    state["capabilities"] = project_capabilities(state)
    assert has_capability(state, CapabilityName.REPOSITORIES)


@pytest.mark.parametrize(
    ("field", "capability"),
    [
        ("code_changes_present", CapabilityName.CODE_CHANGES),
        ("pull_request_expected", CapabilityName.PULL_REQUEST_EXPECTED),
        ("ci_expected", CapabilityName.CI_EXPECTED),
    ],
)
def test_false_boolean_does_not_satisfy_capability(field: str, capability: CapabilityName) -> None:
    assert not has_capability({field: False}, capability)


@pytest.mark.asyncio
async def test_no_contract_preserves_node_behavior() -> None:
    async def node(state: dict) -> dict:
        return {**state, "called": True}

    result = await with_preconditions(node, None)({"ticket_key": "TEST-1"})

    assert result == {"ticket_key": "TEST-1", "called": True}


@pytest.mark.asyncio
async def test_satisfied_contract_runs_sync_node() -> None:
    def node(state: dict) -> dict:
        return {**state, "called": True}

    contract = NodeContract(requires=(Requirement(CapabilityName.WORKSPACE),))
    state = {"workspace_path": "/tmp/workspace"}
    state["capabilities"] = project_capabilities(state)
    result = await with_preconditions(node, contract)(state)

    assert result["called"] is True
    assert "precondition_result" not in result


@pytest.mark.asyncio
async def test_skip_does_not_call_node_and_records_decision() -> None:
    called = False

    async def node(state: dict) -> dict:
        nonlocal called
        called = True
        return state

    contract = NodeContract(
        requires=(Requirement(CapabilityName.CODE_CHANGES, PreconditionAction.SKIP),)
    )
    result = await with_preconditions(node, contract, node_name="create_pr")({})

    assert called is False
    assert result["precondition_result"] == {
        "node": "create_pr",
        "action": "skip",
        "missing": ["code_changes_present"],
        "reason": "missing required capabilities: code_changes_present",
    }
    assert result["precondition_history"] == [result["precondition_result"]]
    assert not result.get("is_blocked", False)
    assert "last_error" not in result


@pytest.mark.asyncio
async def test_block_sets_existing_workflow_control_flag() -> None:
    contract = NodeContract(
        requires=(
            Requirement(
                CapabilityName.PULL_REQUEST,
                PreconditionAction.BLOCK,
                "PR expected but missing",
            ),
        )
    )

    result = await with_preconditions(lambda state: state, contract, node_name="ci_wait")({})

    assert result["is_blocked"] is True
    assert result["last_error"] == "PR expected but missing"


@pytest.mark.asyncio
async def test_safest_policy_wins_for_multiple_missing_requirements() -> None:
    contract = NodeContract(
        requires=(
            Requirement(CapabilityName.CODE_CHANGES, PreconditionAction.SKIP),
            Requirement(CapabilityName.REPOSITORIES, PreconditionAction.BLOCK),
            Requirement(CapabilityName.WORKSPACE, PreconditionAction.RETRY),
        )
    )

    result = await evaluate_preconditions({}, contract)

    assert result.action is PreconditionAction.BLOCK
    assert result.missing == (
        "code_changes_present",
        "repositories_resolved",
        "workspace_ready",
    )


def test_proceed_is_invalid_missing_policy() -> None:
    with pytest.raises(ValueError, match="cannot use the proceed action"):
        Requirement(CapabilityName.WORKSPACE, PreconditionAction.PROCEED)
