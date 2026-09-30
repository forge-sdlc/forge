"""Project node templates are pinned, typed and side-effect free."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from forge.orchestrator.command_handlers import _apply_retry
from forge.workflow.base import merge_node_results
from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler, WorkflowValidationError
from forge.workflow.declarative.composition import resolve_definition
from forge.workflow.declarative.models import (
    NodeDefinition,
    NodeTemplate,
    WorkflowCase,
    WorkflowDefinition,
    WorkflowInclude,
    WorkflowPredicate,
)
from forge.workflow.declarative.node_publication import InMemoryNodePublisher, NodePublisher
from forge.workflow.declarative.predicates import evaluate_predicate
from forge.workflow.declarative.publication import InMemoryDefinitionPublisher
from forge.workflow.declarative.resolver import load_project_workflow
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


@pytest.mark.asyncio
async def test_assessment_resolves_explicit_model_policy(mock_settings, monkeypatch) -> None:
    import forge.config
    from forge.integrations.agents.agent import ForgeAgent

    settings = mock_settings.model_copy(
        update={"model_default": {"connection": "default", "model": mock_settings.llm_model}}
    )
    targets = []

    class Model:
        def with_structured_output(self, _schema):
            return self

        async def ainvoke(self, _prompt):
            return {"outcome": "ready", "summary": "Clear requirements."}

    def create_model(_self, *, model_target):
        targets.append(model_target)
        return Model()

    monkeypatch.setattr(forge.config, "get_settings", lambda: settings)
    monkeypatch.setattr(ForgeAgent, "_create_model", create_model)
    template = NodeTemplate(
        type="agent-assessment-v1",
        outcomes=("ready", "revise"),
        instruction="Assess the spec",
        inputs=("artifact.spec",),
    )
    result = await execute_node(
        template, {"ticket_key": "TEST-1", "spec_content": "Spec"}, "custom_assessment"
    )
    assert targets[0].policy_key == "user_node_assessment"
    assert targets[0].model == mock_settings.llm_model
    assert result["node_results"]["custom_assessment:main"]["outcome"] == "ready"


@pytest.mark.asyncio
async def test_failed_assessment_retry_returns_to_failed_node() -> None:
    definition = workflow(
        {
            "type": "agent-assessment-v1",
            "outcomes": ["ready", "revise"],
            "instruction": "Assess the spec",
            "inputs": ["artifact.spec"],
        }
    )
    compiler = DeclarativeWorkflowCompiler(definition)
    failed = await compiler._guarded_node(
        compiler._node_function("review_artifact"), "review_artifact", terminal=False
    )({"ticket_key": "TEST-1", "current_node": "generate_prd"})
    assert failed["is_blocked"]
    assert "required input" in failed["last_error"]
    retry = _apply_retry(None, failed).state
    assert not retry["is_blocked"]
    assert compiler._entry_route()(retry) == "review_artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["current", "any", "all"])
async def test_user_node_conditions_see_prior_outcomes(scope: str) -> None:
    template = NodeTemplate.model_validate(decision())
    records = {
        f"review_artifact:{branch}": {"attempt": 1, "outcome": "ready"} for branch in ("main", 0, 1)
    }
    state = {"node_results": records, "parallel_total_branches": 2}
    case = WorkflowCase(
        when=WorkflowPredicate(
            fact="node.review_artifact.outcome", scope=scope, op="equals", value="ready"
        ),
        next="generate_prd",
    )

    async def run(value):
        return await execute_node(template, value, "aggregate")

    compiler = DeclarativeWorkflowCompiler(workflow(decision()))
    result = await compiler._guarded_node(
        run, "aggregate", terminal=False, cases=(case,), otherwise="__end__"
    )(state)
    assert result["transition_history"][-1]["target"] == "generate_prd"
    assert compiler._conditional_route((case,), "__end__")(result) == "generate_prd"
    assert result["node_results"]["aggregate:main"]["outcome"] == "ready"
    assert all(result["node_results"][key] == value for key, value in records.items())


def _node_revision(revision: int, ready: str = "ready", revise: str = "revise") -> NodeDefinition:
    spec = decision()
    spec["outcomes"] = [ready, revise]
    spec["rules"][0]["outcome"] = ready
    spec["otherwise"] = revise
    return NodeDefinition.model_validate(
        {
            "apiVersion": "forge/v1",
            "kind": "Node",
            "metadata": {"name": "review", "revision": revision},
            "spec": spec,
        }
    )


async def _activate_consumer(publisher, *, nested: bool) -> None:
    original = builtin_feature_definition()
    review_step = workflow({"source": "project", "name": "review"}).spec.steps["review_artifact"]
    steps = dict(original.spec.steps)
    includes = ()
    if nested:
        fragment = WorkflowDefinition.model_validate(
            {
                "apiVersion": "forge/v1",
                "kind": "Subworkflow",
                "metadata": {"name": "review_fragment", "revision": 1},
                "spec": {
                    "state": "feature",
                    "entry": "review_artifact",
                    "steps": {
                        "review_artifact": {
                            **review_step.model_dump(by_alias=True, exclude_none=True),
                            "cases": [
                                {
                                    "when": review_step.cases[0].when.model_dump(by_alias=True),
                                    "next": "@exit/ready",
                                }
                            ],
                            "otherwise": "@exit/revise",
                        }
                    },
                },
            }
        )
        await publisher.publish(fragment, actor="test", reason="shared review")
        await publisher.activate(fragment, actor="test", reason="shared review")
        includes = (
            WorkflowInclude(
                source="project",
                name="review_fragment",
                exits={"ready": "generate_prd", "revise": "__end__"},
            ),
        )
    else:
        steps["review_artifact"] = review_step
    consumer = original.model_copy(
        update={
            "metadata": original.metadata.model_copy(update={"name": "consumer", "revision": 1}),
            "spec": original.spec.model_copy(
                update={
                    "entry": "review_artifact",
                    "steps": steps,
                    "includes": includes,
                    "resolved_dependencies": (),
                }
            ),
        }
    )
    await publisher.publish(consumer, actor="test", reason="review artifacts")
    await publisher.activate(consumer, actor="test", reason="review artifacts")


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("action", ["activate", "rollback"])
async def test_node_revision_rejects_incompatible_consumers(nested: bool, action: str) -> None:
    nodes = InMemoryNodePublisher("TEST")
    publisher = InMemoryDefinitionPublisher("TEST", node_publisher=nodes)
    active = _node_revision(1 if action == "activate" else 2)
    incompatible = _node_revision(2 if action == "activate" else 1, "pass", "fail")
    for revision in sorted((active, incompatible), key=lambda item: item.metadata.revision):
        await nodes.publish(revision)
    await nodes.activate("review", active.metadata.revision)
    await _activate_consumer(publisher, nested=nested)

    with pytest.raises(ValueError, match="invalidates active consumer.*unknown outcome"):
        await getattr(nodes, action)(
            "review", incompatible.metadata.revision, expected_digest=active.digest
        )
    assert (await nodes.active("review")).digest == active.digest
    await load_project_workflow(None, "TEST", "consumer", definition_reader=publisher)


@pytest.mark.asyncio
async def test_compatible_node_activation_preserves_existing_pin() -> None:
    nodes = InMemoryNodePublisher("TEST")
    publisher = InMemoryDefinitionPublisher("TEST", node_publisher=nodes)
    first, second = _node_revision(1), _node_revision(2)
    await nodes.publish(first)
    await nodes.publish(second)
    await nodes.activate("review", 1)
    await _activate_consumer(publisher, nested=True)
    pinned = await load_project_workflow(None, "TEST", "consumer", definition_reader=publisher)
    await nodes.activate("review", 2, expected_digest=first.digest)
    latest = await load_project_workflow(None, "TEST", "consumer", definition_reader=publisher)
    assert latest.definition.digest != pinned.definition.digest
    assert (
        await resolve_definition(pinned.definition, node_lookup=nodes.active)
    ).digest == pinned.definition.digest
    await nodes.rollback("review", 1, expected_digest=second.digest)
    assert (await nodes.active("review")).digest == first.digest


@pytest.mark.asyncio
async def test_redis_node_activation_checks_consumers_before_pointer_write() -> None:
    """Exercise the Redis publisher's validation wiring without mocking the validator."""
    first, second = _node_revision(1), _node_revision(2, "pass", "fail")
    nodes = InMemoryNodePublisher("TEST")
    publisher = InMemoryDefinitionPublisher("TEST", node_publisher=nodes)
    await nodes.publish(first)
    await nodes.activate("review", 1)
    await _activate_consumer(publisher, nested=False)
    consumer = await publisher.active("consumer")
    data = {
        "forge:node:def:TEST:review:1": first.canonical_json(),
        "forge:node:def:TEST:review:2": second.canonical_json(),
        "forge:node:active:TEST:review": "1",
        "forge:process:def:TEST:consumer:1": consumer.canonical_json(),
        "forge:process:active:TEST:consumer": f"1:{consumer.digest}",
    }
    redis = AsyncMock()
    redis.get.side_effect = data.get
    redis.scan.return_value = (0, ["forge:process:def:TEST:consumer:1"])
    with pytest.raises(ValueError, match="invalidates active consumer.*unknown outcome"):
        await NodePublisher("TEST", redis).activate("review", 2, expected_digest=first.digest)
    redis.eval.assert_not_awaited()
