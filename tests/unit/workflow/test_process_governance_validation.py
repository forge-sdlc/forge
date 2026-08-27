from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from forge.workflow.declarative.builtins import (
    builtin_bug_definition,
    builtin_definitions,
    builtin_feature_definition,
    builtin_task_takeover_definition,
)
from forge.workflow.declarative.capabilities import require_effect_capability
from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.compiler import (
    DeclarativeWorkflowCompiler,
    WorkflowValidationError,
)
from forge.workflow.declarative.models import WorkflowDefinition


def _replace(definition: WorkflowDefinition, **spec_updates) -> WorkflowDefinition:
    value = definition.canonical_dict()
    value["metadata"] = {**value["metadata"], "revision": value["metadata"]["revision"] + 1}
    value["spec"] = {**value["spec"], **spec_updates}
    return WorkflowDefinition.model_validate(value)


def test_every_builtin_station_step_declares_the_registered_contract() -> None:
    for definition in builtin_definitions():
        profile = get_state_profile(definition.spec.state)
        for node_name, binding in profile.station_bindings.items():
            if node_name not in definition.spec.steps:
                continue
            step = definition.spec.steps[node_name]
            assert (step.station_contract, step.station_contract_version) == binding


@pytest.mark.parametrize(
    ("factory", "gate"),
    [
        (builtin_feature_definition, "spec_approval_gate"),
        (builtin_bug_definition, "rca_option_gate"),
        (builtin_task_takeover_definition, "task_plan_approval_gate"),
    ],
)
def test_governed_definitions_cannot_remove_mandatory_gates(factory, gate: str) -> None:
    definition = factory()
    steps = definition.canonical_dict()["spec"]["steps"]
    del steps[gate]
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match=f"mandatory gate '{gate}'"):
        DeclarativeWorkflowCompiler(candidate).validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("mandatoryPolicies", ["unknown-policy"], "unknown mandatory policy"),
        ("extensionPoints", ["arbitrary-python"], "unsupported extension point"),
    ],
)
def test_unknown_governance_capabilities_are_rejected(field, value, message) -> None:
    definition = builtin_feature_definition()
    candidate = _replace(definition, **{field: value})

    with pytest.raises(WorkflowValidationError, match=message):
        DeclarativeWorkflowCompiler(candidate).validate()


def test_unknown_effect_capability_is_rejected() -> None:
    definition = builtin_feature_definition()
    steps = definition.canonical_dict()["spec"]["steps"]
    steps["generate_prd"]["allowedEffects"] = ["shell.*"]
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match="unknown effect capability"):
        DeclarativeWorkflowCompiler(candidate).validate()


def test_registered_station_contract_cannot_be_changed() -> None:
    definition = builtin_feature_definition()
    steps = definition.canonical_dict()["spec"]["steps"]
    steps["generate_prd"]["stationContract"] = "sandbox-execution"
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match="must be"):
        DeclarativeWorkflowCompiler(candidate).validate()


def test_join_requires_multiple_incoming_transitions() -> None:
    definition = builtin_feature_definition()
    steps = definition.canonical_dict()["spec"]["steps"]
    steps["generate_prd"]["join"] = "all"
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match="at least two incoming"):
        DeclarativeWorkflowCompiler(candidate).validate()


def test_dynamic_routes_require_an_explicit_concurrency_limit() -> None:
    definition = builtin_feature_definition().canonical_dict()
    del definition["spec"]["steps"]["task_router"]["maxConcurrency"]

    with pytest.raises(ValidationError, match="explicit maxConcurrency"):
        WorkflowDefinition.model_validate(definition)


@pytest.mark.asyncio
async def test_retry_bound_blocks_before_reinvoking_station() -> None:
    operation = AsyncMock(return_value={"current_node": "work"})
    guarded = DeclarativeWorkflowCompiler._guarded_node(
        operation,
        "work",
        terminal=False,
        retry_bound=2,
    )

    state = await guarded({})
    state = await guarded(state)
    blocked = await guarded(state)

    assert blocked["is_blocked"] is True
    assert "retry bound 2" in blocked["last_error"]
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_compiled_step_enforces_effect_capabilities_at_runtime() -> None:
    async def emit_jira_effect(_state):
        require_effect_capability("jira.comment.create")
        return {}

    allowed = DeclarativeWorkflowCompiler._guarded_node(
        emit_jira_effect,
        "allowed",
        terminal=False,
        allowed_effects=("jira.*",),
    )
    denied = DeclarativeWorkflowCompiler._guarded_node(
        emit_jira_effect,
        "denied",
        terminal=False,
        allowed_effects=("source_control.*",),
    )

    await allowed({})
    with pytest.raises(PermissionError, match="jira.comment.create"):
        await denied({})
