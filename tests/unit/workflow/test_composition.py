"""Reusable process definitions preserve a flat, immutable runtime graph."""

from __future__ import annotations

import pytest

from forge.workflow.declarative.builtins import builtin_feature_definition, load_builtin_source
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.composition import resolve_definition, validate_subworkflow
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import WorkflowInclude
from forge.workflow.declarative.publication import InMemoryDefinitionPublisher
from forge.workflow.declarative.resolver import load_project_workflow


def _project_consumer(name: str = "shared_feature"):
    original = builtin_feature_definition()
    fragment = load_builtin_source("github_pr_review")
    spec = original.spec.model_copy(
        update={
            "steps": {
                key: value
                for key, value in original.spec.steps.items()
                if key not in fragment.spec.steps
            },
            "includes": (
                WorkflowInclude(
                    source="project",
                    name="github_pr_review",
                    exits={
                        "blocked": "escalate_blocked",
                        "review": "human_review_gate",
                        "setup": "setup_workspace",
                    },
                ),
            ),
            "resolved_dependencies": (),
        }
    )
    return original.model_copy(
        update={
            "metadata": original.metadata.model_copy(update={"name": name, "revision": 1}),
            "spec": spec,
        }
    )


@pytest.mark.asyncio
async def test_project_dependency_update_affects_new_runs_and_preserves_pin() -> None:
    publisher = InMemoryDefinitionPublisher("PROJ")
    fragment = load_builtin_source("github_pr_review")
    validate_subworkflow(fragment)
    await publisher.publish(fragment, actor="admin", reason="shared path")
    await publisher.activate(fragment, actor="admin", reason="enable shared path")
    consumer = _project_consumer()
    await publisher.publish(consumer, actor="admin", reason="use shared path")
    await publisher.activate(consumer, actor="admin", reason="enable process")

    first = await load_project_workflow(
        None, "PROJ", consumer.metadata.name, definition_reader=publisher
    )
    DeclarativeWorkflowCompiler(first.definition).validate_for_publication()
    assert first.definition.spec.steps["create_pr"] == fragment.spec.steps["create_pr"].model_copy(
        update={
            "branches": {
                "escalate_blocked": "escalate_blocked",
                "teardown_workspace": "teardown_workspace",
            }
        }
    )

    updated_steps = dict(fragment.spec.steps)
    updated_steps["attempt_ci_fix"] = updated_steps["attempt_ci_fix"].model_copy(
        update={"retry_bound": 4}
    )
    second_fragment = fragment.model_copy(
        update={
            "metadata": fragment.metadata.model_copy(update={"revision": 2}),
            "spec": fragment.spec.model_copy(update={"steps": updated_steps}),
        }
    )
    await publisher.publish(second_fragment, actor="admin", reason="new retry bound")
    await publisher.activate(
        second_fragment, actor="admin", reason="roll out", expected_active_digest=fragment.digest
    )
    second = await load_project_workflow(
        None, "PROJ", consumer.metadata.name, definition_reader=publisher
    )
    assert second.definition.digest != first.definition.digest
    assert second.definition.spec.steps["attempt_ci_fix"].retry_bound == 4

    pinned = await load_project_workflow(
        None,
        "PROJ",
        consumer.metadata.name,
        pinned_revision=first.definition.metadata.revision,
        pinned_digest=first.definition.digest,
        pinned_definition=first.definition.canonical_dict(),
        definition_reader=publisher,
    )
    assert pinned.definition.digest == first.definition.digest


@pytest.mark.asyncio
async def test_full_workflow_normal_completion_returns_to_caller() -> None:
    child = load_builtin_source("feature")
    child = child.model_copy(
        update={
            "metadata": child.metadata.model_copy(update={"name": "child"}),
            "spec": child.spec.model_copy(
                update={
                    "entry": "generate_prd",
                    "steps": {
                        "generate_prd": child.spec.steps["generate_prd"].model_copy(
                            update={"next": "__end__", "route": None, "branches": {}}
                        )
                    },
                    "resolved_dependencies": (),
                }
            ),
        }
    )
    parent = _project_consumer("outer")
    parent = parent.model_copy(
        update={
            "spec": parent.spec.model_copy(
                update={
                    "entry": "generate_spec",
                    "steps": {
                        "generate_spec": child.spec.steps["generate_prd"].model_copy(
                            update={"next": "generate_prd"}
                        ),
                        "answer_question": child.spec.steps["generate_prd"].model_copy(
                            update={"next": "__end__"}
                        ),
                    },
                    "includes": (
                        WorkflowInclude(source="project", name="child", returnTo="answer_question"),
                    ),
                }
            )
        }
    )

    async def lookup(name: str):
        return child if name == "child" else None

    resolved = await resolve_definition(parent, lookup)
    assert resolved.spec.steps["generate_prd"].next == "answer_question"
    DeclarativeWorkflowCompiler(resolved).validate()


@pytest.mark.asyncio
async def test_missing_exit_and_dependency_cycles_are_rejected() -> None:
    consumer = _project_consumer()
    fragment = load_builtin_source("github_pr_review")
    bad_include = consumer.spec.includes[0].model_copy(
        update={"exits": {"review": "human_review_gate"}}
    )
    bad = consumer.model_copy(
        update={"spec": consumer.spec.model_copy(update={"includes": (bad_include,)})}
    )

    async def lookup(name: str):
        return fragment if name == "github_pr_review" else None

    with pytest.raises(ValueError, match="requires exactly these exits"):
        await resolve_definition(bad, lookup)

    recursive = consumer.model_copy(
        update={
            "spec": consumer.spec.model_copy(
                update={
                    "includes": (
                        WorkflowInclude(
                            source="project", name=consumer.metadata.name, returnTo="__end__"
                        ),
                    )
                }
            )
        }
    )

    async def self_lookup(name: str):
        return recursive if name == recursive.metadata.name else None

    with pytest.raises(ValueError, match="dependency cycle"):
        await resolve_definition(recursive, self_lookup)


@pytest.mark.asyncio
async def test_dependency_activation_rejects_a_graph_that_breaks_active_consumer() -> None:
    publisher = InMemoryDefinitionPublisher("PROJ")
    fragment = load_builtin_source("github_pr_review")
    await publisher.publish(fragment, actor="admin", reason="shared path")
    await publisher.activate(fragment, actor="admin", reason="enable shared path")
    consumer = _project_consumer()
    await publisher.publish(consumer, actor="admin", reason="use shared path")
    await publisher.activate(consumer, actor="admin", reason="enable process")

    broken_steps = dict(fragment.spec.steps)
    ci_branches = dict(broken_steps["ci_evaluator"].branches)
    ci_branches["escalate_blocked"] = "@exit/urgent"
    broken_steps["ci_evaluator"] = broken_steps["ci_evaluator"].model_copy(
        update={"branches": ci_branches}
    )
    broken = fragment.model_copy(
        update={
            "metadata": fragment.metadata.model_copy(update={"revision": 2}),
            "spec": fragment.spec.model_copy(update={"steps": broken_steps}),
        }
    )
    await publisher.publish(broken, actor="admin", reason="new exit")
    with pytest.raises(ValueError, match="requires exactly these exits"):
        await publisher.activate(
            broken, actor="admin", reason="bad rollout", expected_active_digest=fragment.digest
        )
    assert (await publisher.active("github_pr_review")).digest == fragment.digest


@pytest.mark.asyncio
async def test_collision_and_state_profile_mismatch_are_rejected() -> None:
    fragment = load_builtin_source("github_pr_review")
    consumer = _project_consumer()

    async def lookup(name: str):
        return fragment if name == fragment.metadata.name else None

    colliding_steps = dict(consumer.spec.steps)
    colliding_steps["create_pr"] = fragment.spec.steps["create_pr"]
    colliding = consumer.model_copy(
        update={"spec": consumer.spec.model_copy(update={"steps": colliding_steps})}
    )
    with pytest.raises(ValueError, match="collides"):
        await resolve_definition(colliding, lookup)

    incompatible = fragment.model_copy(
        update={"spec": fragment.spec.model_copy(update={"compatible_states": ()})}
    )

    async def incompatible_lookup(name: str):
        return incompatible if name == fragment.metadata.name else None

    bug_caller = consumer.model_copy(
        update={"spec": consumer.spec.model_copy(update={"state": "bug"})}
    )
    with pytest.raises(ValueError, match="does not support 'bug'"):
        await resolve_definition(bug_caller, incompatible_lookup)


@pytest.mark.asyncio
async def test_nested_subworkflows_bind_exits_through_parent() -> None:
    def definition(kind: str, name: str, entry: str, steps: dict, includes: list | None = None):
        return load_workflow_value(
            {
                "apiVersion": "forge/v1",
                "kind": kind,
                "metadata": {"name": name, "revision": 1},
                "spec": {
                    "state": "feature",
                    "entry": entry,
                    "steps": steps,
                    "includes": includes or [],
                },
            }
        )

    inner = definition(
        "Subworkflow", "inner", "generate_spec", {"generate_spec": {"next": "@exit/done"}}
    )
    middle = definition(
        "Subworkflow",
        "middle",
        "generate_prd",
        {"generate_prd": {"next": "generate_spec"}},
        [{"source": "project", "name": "inner", "exits": {"done": "@exit/done"}}],
    )
    outer = definition(
        "Workflow",
        "outer",
        "generate_prd",
        {"answer_question": {"next": "__end__"}},
        [{"source": "project", "name": "middle", "exits": {"done": "answer_question"}}],
    )

    async def lookup(name: str):
        return {"inner": inner, "middle": middle}.get(name)

    resolved = await resolve_definition(outer, lookup)
    assert resolved.spec.steps["generate_spec"].next == "answer_question"
    DeclarativeWorkflowCompiler(resolved).validate()
