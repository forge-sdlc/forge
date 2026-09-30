"""Project node templates are pinned, typed and side-effect free."""

from __future__ import annotations

import pytest

from forge.workflow.base import merge_node_results
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler, WorkflowValidationError
from forge.workflow.declarative.composition import resolve_definition
from forge.workflow.declarative.models import NodeDefinition, NodeTemplate, WorkflowDefinition
from forge.workflow.declarative.node_publication import InMemoryNodePublisher
from forge.workflow.declarative.predicates import evaluate_predicate
from forge.workflow.declarative.user_nodes import execute_node, select_inputs


def decision(otherwise: str = "revise") -> dict:
    return {
        "type": "decision-v1",
        "outcomes": ["ready", "revise"],
        "rules": [
            {
                "when": {"fact": "workflow.has_error", "op": "equals", "value": False},
                "outcome": "ready",
            }
        ],
        "otherwise": otherwise,
    }


def workflow(node: dict) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "artifact-review", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "review_artifact",
                "steps": {
                    "review_artifact": {
                        "node": node,
                        "cases": [
                            {
                                "when": {
                                    "fact": "node.review_artifact.outcome",
                                    "op": "equals",
                                    "value": "ready",
                                },
                                "next": "generate_prd",
                            }
                        ],
                        "otherwise": "__end__",
                    },
                    "generate_prd": {"next": "__end__"},
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_inline_decision_result_routes_and_records_attempt() -> None:
    definition = workflow(decision())
    compiler = DeclarativeWorkflowCompiler(definition)
    compiler.build_graph().compile()
    result = await execute_node(
        definition.spec.steps["review_artifact"].node,
        {
            "ticket_key": "TEST-1",
            "last_error": None,
            "workflow_node_attempts": {"review_artifact": 2},
        },
        "review_artifact",
    )
    assert result["node_results"]["review_artifact:main"]["attempt"] == 3
    assert result["node_results"]["review_artifact:main"]["outcome"] == "ready"
    assert (
        compiler._conditional_route(definition.spec.steps["review_artifact"].cases, "__end__")(
            result
        )
        == "generate_prd"
    )


@pytest.mark.asyncio
async def test_project_reference_pins_active_revision() -> None:
    publisher = InMemoryNodePublisher("TEST")
    first = NodeDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Node",
            "metadata": {"name": "review", "revision": 1},
            "spec": decision(),
        }
    )
    await publisher.publish(first)
    await publisher.activate("review", 1)
    source = workflow({"source": "project", "name": "review"})
    pinned = await resolve_definition(source, node_lookup=publisher.active)
    assert pinned.spec.steps["review_artifact"].node.otherwise == "revise"
    second = first.model_copy(
        update={
            "metadata": first.metadata.model_copy(update={"revision": 2}),
            "spec": NodeTemplate.model_validate(
                {
                    **decision(),
                    "rules": [
                        {
                            "when": {"fact": "workflow.has_error", "op": "equals", "value": False},
                            "outcome": "revise",
                        }
                    ],
                }
            ),
        }
    )
    await publisher.publish(second)
    await publisher.activate("review", 2, expected_digest=first.digest)
    assert (await resolve_definition(source, node_lookup=publisher.active)).digest != pinned.digest
    assert (await resolve_definition(pinned, node_lookup=publisher.active)).digest == pinned.digest


def test_unknown_outcome_and_missing_input_fail_closed() -> None:
    definition = workflow(decision())
    bad = definition.model_copy(
        update={
            "spec": definition.spec.model_copy(
                update={
                    "steps": {
                        **definition.spec.steps,
                        "review_artifact": definition.spec.steps["review_artifact"].model_copy(
                            update={
                                "cases": (
                                    definition.spec.steps["review_artifact"]
                                    .cases[0]
                                    .model_copy(
                                        update={
                                            "when": definition.spec.steps["review_artifact"]
                                            .cases[0]
                                            .when.model_copy(update={"value": "unknown"})
                                        }
                                    ),
                                )
                            }
                        ),
                    }
                }
            )
        }
    )
    with pytest.raises(WorkflowValidationError, match="unknown outcome"):
        DeclarativeWorkflowCompiler(bad).validate()
    template = NodeTemplate(
        type="agent-assessment-v1",
        outcomes=("ready", "revise"),
        instruction="Assess artifact",
        inputs=("artifact.spec",),
    )
    with pytest.raises(ValueError, match="unavailable"):
        select_inputs(template, {"ticket_key": "TEST-1"})


def test_parallel_results_merge_and_scope() -> None:
    left = {"review_artifact:0": {"attempt": 1, "outcome": "ready"}}
    right = {"review_artifact:1": {"attempt": 1, "outcome": "revise"}}
    merged = merge_node_results(left, right)
    predicate = (
        workflow(decision())
        .spec.steps["review_artifact"]
        .cases[0]
        .when.model_copy(update={"scope": "any"})
    )
    assert evaluate_predicate(predicate, {"node_results": merged, "parallel_total_branches": 2})
    assert not evaluate_predicate(
        predicate.model_copy(update={"scope": "all"}),
        {"node_results": merged, "parallel_total_branches": 2},
    )
    with pytest.raises(ValueError, match="missing"):
        evaluate_predicate(predicate, {"node_results": left, "parallel_total_branches": 2})


@pytest.mark.asyncio
async def test_agent_assessment_receives_only_selected_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import forge.config
    import forge.integrations.agents.agent
    import forge.model_policy

    prompts = []

    class FakeModel:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        async def ainvoke(self, prompt):
            prompts.append(json.loads(prompt))
            return {"outcome": "ready", "summary": "Acceptance criteria are clear."}

    class FakeAgent:
        def __init__(self, settings):
            pass

        def _create_model(self, *, model_target):
            _ = model_target
            return FakeModel()

    async def target(*args):
        _ = args
        return object()

    monkeypatch.setattr(forge.config, "get_settings", lambda: object())
    monkeypatch.setattr(forge.model_policy, "resolve_model_target_for_project", target)
    monkeypatch.setattr(forge.integrations.agents.agent, "ForgeAgent", FakeAgent)
    template = NodeTemplate(
        type="agent-assessment-v1",
        outcomes=("ready", "revise"),
        instruction="Review the specification",
        inputs=("artifact.spec",),
    )
    result = await execute_node(
        template,
        {"ticket_key": "DEMO-1", "spec_content": "Clear requirements", "secret": "private"},
        "review_artifact",
    )
    assert prompts[0]["inputs"] == {"artifact.spec": "Clear requirements"}
    assert "private" not in json.dumps(prompts)
    assert (
        result["node_results"]["review_artifact:main"]["summary"]
        == "Acceptance criteria are clear."
    )
