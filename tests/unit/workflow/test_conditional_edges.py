"""Conditional topology uses typed facts and remains revision-safe."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler, WorkflowValidationError
from forge.workflow.declarative.composition import resolve_definition
from forge.workflow.declarative.manifest import build_process_manifest, compare_process_definitions
from forge.workflow.declarative.models import WorkflowDefinition


def _definition(steps: dict, *, revision: int = 1) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "conditional-feature", "revision": revision},
            "spec": {"state": "feature", "entry": "generate_prd", "steps": steps},
        }
    )


def _steps(target: str = "generate_spec") -> dict:
    return {
        "generate_prd": {
            "cases": [
                {
                    "when": {
                        "all": [
                            {"fact": "workflow.has_error", "op": "equals", "value": True},
                            {"not": {"fact": "workflow.paused", "op": "equals", "value": True}},
                        ]
                    },
                    "next": "escalate_blocked",
                },
                {
                    "when": {"fact": "workflow.paused", "op": "equals", "value": False},
                    "next": target,
                },
            ],
            "otherwise": "__end__",
        },
        "escalate_blocked": {"next": "__end__"},
        "generate_spec": {"next": "__end__"},
    }


def test_conditional_edges_compile_and_select_first_matching_case() -> None:
    definition = _definition(_steps())
    compiler = DeclarativeWorkflowCompiler(definition)
    compiler.build_graph().compile()
    route = compiler._conditional_route(definition.spec.steps["generate_prd"].cases, "__end__")

    assert route({"last_error": "failed", "is_paused": False}) == "escalate_blocked"
    assert route({"last_error": None, "is_paused": False}) == "generate_spec"
    assert route({"is_paused": True}) == "__end__"
    assert route({"is_blocked": True}) == "__end__"


@pytest.mark.asyncio
async def test_condition_selection_is_recorded_with_actual_target() -> None:
    async def operation(state: dict) -> dict:
        return {**state, "last_error": "failed", "is_paused": False}

    step = _definition(_steps()).spec.steps["generate_prd"]
    guarded = DeclarativeWorkflowCompiler._guarded_node(
        operation, "generate_prd", terminal=False, cases=step.cases, otherwise=step.otherwise
    )
    result = await guarded({"ticket_key": "TEST-1"})

    assert result["transition_history"][-1]["case"] == "0"
    assert result["transition_history"][-1]["target"] == "escalate_blocked"


@pytest.mark.parametrize(
    ("predicate", "message"),
    [
        ({"fact": "ticket_key", "op": "equals", "value": "TEST-1"}, "unknown conditional fact"),
        ({"fact": "workflow.paused", "op": "equals", "value": 1}, "requires bool values"),
        ({"fact": "ci.status", "op": "in", "value": ["invented"]}, "unknown value"),
    ],
)
def test_untrusted_or_mistyped_predicates_are_rejected(predicate: dict, message: str) -> None:
    steps = _steps()
    steps["generate_prd"]["cases"][0]["when"] = predicate
    with pytest.raises(WorkflowValidationError, match=message):
        DeclarativeWorkflowCompiler(_definition(steps)).validate()


def test_conditional_shape_requires_otherwise_and_excludes_router() -> None:
    steps = _steps()
    del steps["generate_prd"]["otherwise"]
    with pytest.raises(ValidationError, match="require otherwise"):
        _definition(steps)

    steps = _steps()
    steps["generate_prd"]["route"] = "route_after_generation"
    with pytest.raises(ValidationError, match="exactly one"):
        _definition(steps)

    steps = _steps()
    steps["generate_prd"]["branches"] = {"ignored": "missing_step"}
    with pytest.raises(ValidationError, match="branches are only valid with 'route'"):
        _definition(steps)


def test_manifest_and_diff_include_predicates_even_when_target_is_unchanged() -> None:
    previous = _definition(_steps())
    steps = _steps()
    steps["generate_prd"]["cases"][1]["when"]["value"] = True
    current = _definition(steps, revision=2)

    manifest = build_process_manifest(previous)
    assert any(t.source == "generate_prd" and t.condition for t in manifest.transitions)
    impact = compare_process_definitions(previous, current)
    assert "generate_prd" in impact.changed_transitions
    assert "generate_prd" in impact.routing_changes
    assert not impact.compatible_for_in_flight


@pytest.mark.asyncio
async def test_subworkflow_exit_binds_conditional_targets() -> None:
    child = WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Subworkflow",
            "metadata": {"name": "conditional-child", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {
                    "generate_prd": {
                        "cases": [
                            {
                                "when": {
                                    "fact": "workflow.has_error",
                                    "op": "equals",
                                    "value": True,
                                },
                                "next": "@exit/error",
                            }
                        ],
                        "otherwise": "@exit/done",
                    }
                },
            },
        }
    )
    parent = WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "conditional-feature", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {
                    "escalate_blocked": {"next": "__end__"},
                    "generate_spec": {"next": "__end__"},
                },
                "includes": [
                    {
                        "source": "project",
                        "name": "conditional-child",
                        "exits": {"error": "escalate_blocked", "done": "generate_spec"},
                    }
                ],
            },
        }
    )

    async def lookup(_name: str) -> WorkflowDefinition:
        return child

    resolved = await resolve_definition(parent, lookup)
    DeclarativeWorkflowCompiler(resolved).build_graph().compile()
    step = resolved.spec.steps["generate_prd"]
    assert step.cases[0].next == "escalate_blocked"
    assert step.otherwise == "generate_spec"


def test_protected_gate_keeps_trusted_router() -> None:
    raw = builtin_feature_definition().canonical_dict()
    raw["metadata"]["revision"] += 1
    gate = raw["spec"]["steps"]["prd_approval_gate"]
    gate.pop("route")
    gate.pop("branches")
    gate["cases"] = [
        {
            "when": {"fact": "workflow.paused", "op": "equals", "value": False},
            "next": "generate_spec",
        }
    ]
    gate["otherwise"] = "__end__"
    with pytest.raises(WorkflowValidationError, match="protected gate"):
        DeclarativeWorkflowCompiler(
            WorkflowDefinition.model_validate(raw)
        ).validate_for_publication()


def test_publishable_builtin_can_replace_ci_router_with_authored_conditions() -> None:
    raw = builtin_feature_definition().canonical_dict()
    raw["metadata"]["revision"] += 1
    ci = raw["spec"]["steps"]["ci_evaluator"]
    ci.pop("route")
    ci.pop("branches")
    ci["cases"] = [
        {
            "when": {"fact": "ci.status", "op": "in", "value": ["failed", "blocked", "no_prs"]},
            "next": "escalate_blocked",
        },
        {
            "when": {"fact": "ci.status", "op": "equals", "value": "fixing"},
            "next": "attempt_ci_fix",
        },
    ]
    ci["otherwise"] = "human_review_gate"

    candidate = WorkflowDefinition.model_validate(raw)
    DeclarativeWorkflowCompiler(candidate).validate_for_publication()
    DeclarativeWorkflowCompiler(candidate).build_graph().compile()


@pytest.mark.asyncio
async def test_included_workflow_keeps_conditional_pause_and_rewrites_fixed_completion() -> None:
    child = WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "conditional-child", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {
                    "generate_prd": {
                        "cases": [
                            {
                                "when": {"fact": "workflow.paused", "op": "equals", "value": True},
                                "next": "__end__",
                            }
                        ],
                        "otherwise": "generate_spec",
                    },
                    "generate_spec": {"next": "__end__"},
                },
            },
        }
    )
    parent = WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "conditional-parent", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "answer_question",
                "steps": {
                    "answer_question": {"next": "generate_prd"},
                    "teardown_workspace": {"next": "__end__"},
                },
                "includes": [
                    {
                        "source": "project",
                        "name": "conditional-child",
                        "returnTo": "teardown_workspace",
                    }
                ],
            },
        }
    )

    async def lookup(_name: str) -> WorkflowDefinition:
        return child

    resolved = await resolve_definition(parent, lookup)
    assert resolved.spec.steps["generate_prd"].cases[0].next == "__end__"
    assert resolved.spec.steps["generate_spec"].next == "teardown_workspace"
    DeclarativeWorkflowCompiler(resolved).validate()

    conditional_only = child.model_copy(
        update={
            "spec": child.spec.model_copy(
                update={
                    "steps": {
                        "generate_prd": child.spec.steps["generate_prd"].model_copy(
                            update={"otherwise": "__end__"}
                        )
                    }
                }
            )
        }
    )

    async def lookup_without_completion(_name: str) -> WorkflowDefinition:
        return conditional_only

    with pytest.raises(ValueError, match="no fixed normal completion"):
        await resolve_definition(parent, lookup_without_completion)
