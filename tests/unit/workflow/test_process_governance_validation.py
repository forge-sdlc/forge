from unittest.mock import AsyncMock

import pytest
from langgraph.types import Send
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
from forge.workflow.declarative.effect_catalog import NodeEffectPolicy
from forge.workflow.declarative.models import WorkflowDefinition


def _replace(definition: WorkflowDefinition, **spec_updates) -> WorkflowDefinition:
    value = definition.canonical_dict()
    value["metadata"] = {**value["metadata"], "revision": value["metadata"]["revision"] + 1}
    value["spec"] = {**value["spec"], **spec_updates}
    return WorkflowDefinition.model_validate(value)


def test_every_builtin_station_step_derives_the_registered_contract() -> None:
    for definition in builtin_definitions():
        profile = get_state_profile(definition.spec.state)
        for node_name, binding in profile.station_bindings.items():
            if node_name not in definition.spec.steps:
                continue
            step = definition.spec.steps[node_name]
            assert (step.station_contract, step.station_contract_version) == (None, None)
            assert profile.station_bindings[node_name] == binding


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
        DeclarativeWorkflowCompiler(candidate).validate_for_publication()


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
    steps["generate_prd"]["allowedEffects"] = ["shell.execute"]
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match="unknown effect capability"):
        DeclarativeWorkflowCompiler(candidate).validate()


def test_effect_capabilities_are_inherited_from_the_node_catalog() -> None:
    candidate = WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "inherited-effects", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {"generate_prd": {"next": "__end__"}},
            },
        }
    )
    compiler = DeclarativeWorkflowCompiler(candidate)

    compiler.validate()

    assert candidate.spec.steps["generate_prd"].allowed_effects is None
    assert "jira.comment" in compiler.effective_effects("generate_prd")


def test_explicit_effects_cannot_remove_a_required_capability() -> None:
    policy = NodeEffectPolicy(
        required=frozenset({"jira.comment"}),
        optional=frozenset({"jira.labels"}),
    )

    with pytest.raises(ValueError, match="omits required effect capability 'jira.comment'"):
        policy.resolve(("jira.labels",))

    assert policy.resolve(("jira.comment",)) == ("jira.comment",)


def test_registered_station_contract_cannot_be_changed() -> None:
    definition = builtin_feature_definition()
    steps = definition.canonical_dict()["spec"]["steps"]
    steps["generate_prd"]["stationContract"] = "sandbox-execution"
    steps["generate_prd"]["stationContractVersion"] = "1.0"
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


def test_publication_validates_complete_router_outcome_contract() -> None:
    definition = builtin_feature_definition()
    steps = definition.canonical_dict()["spec"]["steps"]
    del steps["prd_approval_gate"]["branches"]["answer_question"]
    candidate = _replace(definition, steps=steps)

    with pytest.raises(WorkflowValidationError, match="omits router outcome 'answer_question'"):
        DeclarativeWorkflowCompiler(candidate).validate_for_publication()


def test_legacy_extension_declaration_cannot_authorize_router_outcomes() -> None:
    definition = builtin_feature_definition()
    raw = definition.canonical_dict()
    raw["metadata"]["revision"] += 1
    raw["spec"]["extensionPoints"] = ["routing-branches"]
    raw["spec"]["steps"]["prd_approval_gate"]["branches"]["invented"] = "generate_spec"
    candidate = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError, match="unregistered router outcome 'invented'"):
        DeclarativeWorkflowCompiler(candidate).validate_for_publication()


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
        allowed_effects=("jira.comment",),
    )
    denied = DeclarativeWorkflowCompiler._guarded_node(
        emit_jira_effect,
        "denied",
        terminal=False,
        allowed_effects=("source_control.review",),
    )

    await allowed({})
    with pytest.raises(PermissionError, match="jira.comment.create"):
        await denied({})


@pytest.mark.asyncio
async def test_dynamic_router_enforces_targets_and_concurrency() -> None:
    too_many = DeclarativeWorkflowCompiler._guarded_dynamic_router(
        lambda _state: [Send("worker", {}), Send("worker", {})],
        {"worker"},
        1,
    )
    undeclared = DeclarativeWorkflowCompiler._guarded_dynamic_router(
        lambda _state: Send("arbitrary", {}),
        {"worker"},
        1,
    )

    with pytest.raises(WorkflowValidationError, match="maximum is 1"):
        await too_many({})
    with pytest.raises(WorkflowValidationError, match="undeclared target"):
        await undeclared({})
