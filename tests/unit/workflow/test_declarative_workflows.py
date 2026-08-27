from __future__ import annotations

from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError

from forge.orchestrator.worker import OrchestratorWorker
from forge.workflow.declarative.builtins import builtin_definitions, builtin_feature_definition
from forge.workflow.declarative.cli import cmd_workflow
from forge.workflow.declarative.compiler import (
    DeclarativeWorkflowCompiler,
    WorkflowValidationError,
)
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.manifest import (
    ProcessNodeKind,
    build_process_manifest,
    compare_process_definitions,
    render_mermaid,
)
from forge.workflow.declarative.models import WORKFLOW_PROPERTY_PREFIX
from forge.workflow.declarative.publication import InMemoryDefinitionPublisher
from forge.workflow.declarative.resolver import (
    load_project_workflow,
    selected_workflow_name,
)
from forge.workflow.declarative.workflow import DeclarativeWorkflow
from forge.workflow.preconditions import (
    CapabilityName,
    NodeContract,
    PreconditionAction,
    Requirement,
)
from forge.workflow.registry import create_default_router


def definition_value(
    *, revision: int = 1, steps: dict | None = None, state: str = "feature"
) -> dict:
    return {
        "apiVersion": "forge/v1",
        "kind": "Workflow",
        "metadata": {"name": "short-feature", "revision": revision},
        "spec": {
            "state": state,
            "entry": "generate_prd",
            "steps": steps or {"generate_prd": {"next": "__end__"}},
        },
    }


def test_loads_strict_definition_and_computes_stable_digest() -> None:
    first = load_workflow_value(definition_value())
    second = load_workflow_value(definition_value())

    assert first.digest == second.digest
    assert first.property_key == f"{WORKFLOW_PROPERTY_PREFIX}short-feature"


def test_builtin_feature_golden_path_is_valid_and_inspectable() -> None:
    definition = builtin_feature_definition()
    DeclarativeWorkflowCompiler(definition).validate()
    manifest = build_process_manifest(definition)

    assert definition.metadata.name == "feature"
    assert len(manifest.nodes) == 32
    assert any(node.name == "task_router" and node.station_contract for node in manifest.nodes)
    assert any(node.name == "prd_approval_gate" and node.kind == "gate" for node in manifest.nodes)


def test_every_supported_golden_path_uses_the_versioned_definition_compiler() -> None:
    definitions = builtin_definitions()

    assert {item.metadata.name for item in definitions} == {"feature", "bug", "task_takeover"}
    for definition in definitions:
        DeclarativeWorkflowCompiler(definition).validate()
        graph = DeclarativeWorkflowCompiler(definition).build_graph()
        assert graph is not None
        assert definition.spec.mandatory_policies == ("forge-contracts-v1",)
        assert all(
            "forge-contracts-v1" in step.required_policies
            for step in definition.spec.steps.values()
        )


def test_default_router_has_no_python_topology_workflow_runtime() -> None:
    router = create_default_router()

    assert router._workflows  # noqa: SLF001 - architecture assertion
    assert all(issubclass(item, DeclarativeWorkflow) for item in router._workflows)  # noqa: SLF001


@pytest.mark.asyncio
async def test_publication_is_immutable_and_activation_is_explicit() -> None:
    publisher = InMemoryDefinitionPublisher()
    first = builtin_feature_definition()

    published = await publisher.publish(first, actor="platform", reason="initial publication")
    assert published.activated is False
    assert await publisher.active(first.metadata.name) is None

    activated = await publisher.activate(
        first.metadata.name,
        first.metadata.revision,
        actor="platform",
        reason="initial rollout",
    )
    assert activated.activated is True
    assert (await publisher.active(first.metadata.name)).digest == first.digest

    changed = first.canonical_dict()
    changed["metadata"]["description"] = "changed without a revision"
    with pytest.raises(ValueError, match="immutable"):
        await publisher.publish(
            load_workflow_value(changed), actor="platform", reason="invalid mutation"
        )


def test_rejects_unknown_fields() -> None:
    value = definition_value()
    value["spec"]["execute"] = "os.system"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_workflow_value(value)


def test_rejects_property_larger_than_jira_limit() -> None:
    value = definition_value(revision=2)
    value["spec"]["resume"] = {
        "fromRevisions": {"1": {f"retired_node_{index}": "generate_prd" for index in range(2_000)}}
    }

    with pytest.raises(ValueError, match="32768"):
        load_workflow_value(value)


def test_compiles_allowlisted_node() -> None:
    definition = load_workflow_value(definition_value())
    graph = DeclarativeWorkflowCompiler(definition).build_graph().compile()

    assert "generate_prd" in graph.nodes
    assert "_forge_entry" in graph.nodes


def test_process_manifest_exposes_stations_gates_and_transitions() -> None:
    value = definition_value(
        steps={
            "task_router": {"next": "prd_approval_gate"},
            "prd_approval_gate": {
                "route": "route_prd_approval",
                "branches": {"revise": "task_router", "approved": "__end__"},
            },
        }
    )
    value["spec"]["entry"] = "task_router"

    manifest = build_process_manifest(load_workflow_value(value))

    nodes = {node.name: node for node in manifest.nodes}
    assert nodes["task_router"].kind is ProcessNodeKind.STATION
    assert nodes["task_router"].station_contract == "task-routing"
    assert nodes["prd_approval_gate"].kind is ProcessNodeKind.GATE
    assert any(
        edge.source == "prd_approval_gate"
        and edge.outcome == "approved"
        and edge.target == "__end__"
        for edge in manifest.transitions
    )
    assert manifest.digest == load_workflow_value(value).digest


def test_mermaid_uses_same_manifest_and_labels_routes() -> None:
    manifest = build_process_manifest(load_workflow_value(definition_value()))

    rendered = render_mermaid(manifest)

    assert rendered.startswith("flowchart TD")
    assert "__start__([start]) --> generate_prd" in rendered
    assert "generate_prd --> __end__" in rendered


def test_revision_diff_reports_missing_resume_mapping() -> None:
    previous = load_workflow_value(definition_value(revision=1))
    current_value = definition_value(revision=2, steps={"generate_spec": {"next": "__end__"}})
    current_value["spec"]["entry"] = "generate_spec"
    current = load_workflow_value(current_value)

    impact = compare_process_definitions(previous, current)

    assert impact.removed_nodes == ("generate_prd",)
    assert impact.added_nodes == ("generate_spec",)
    assert impact.missing_resume_mappings == ("generate_prd",)
    assert impact.compatible_for_in_flight is False


def test_revision_diff_accepts_explicit_resume_mapping() -> None:
    previous = load_workflow_value(definition_value(revision=1))
    current_value = definition_value(revision=2, steps={"generate_spec": {"next": "__end__"}})
    current_value["spec"]["entry"] = "generate_spec"
    current_value["spec"]["resume"] = {"fromRevisions": {"1": {"generate_prd": "generate_spec"}}}

    impact = compare_process_definitions(previous, load_workflow_value(current_value))

    assert impact.missing_resume_mappings == ()
    assert impact.compatible_for_in_flight is True


@pytest.mark.asyncio
async def test_runtime_transition_budget_blocks_before_side_effect() -> None:
    value = definition_value(
        steps={
            "prd_approval_gate": {
                "route": "route_prd_approval",
                "branches": {"__end__": "__end__"},
            }
        }
    )
    value["spec"]["entry"] = "prd_approval_gate"
    graph = DeclarativeWorkflowCompiler(load_workflow_value(value)).build_graph().compile()

    result = await graph.ainvoke(
        {
            "ticket_key": "PROJ-1",
            "current_node": "entry",
            "workflow_transition_count": 500,
        }
    )

    assert result["is_blocked"] is True
    assert "exceeded 500 transitions" in result["last_error"]


@pytest.mark.asyncio
async def test_guarded_node_enforces_opt_in_contract_before_side_effect() -> None:
    called = False

    async def create_pr(state: dict) -> dict:
        nonlocal called
        called = True
        return state

    contract = NodeContract(
        requires=(Requirement(CapabilityName.CODE_CHANGES, PreconditionAction.SKIP),)
    )
    node = DeclarativeWorkflowCompiler._guarded_node(
        create_pr,
        "create_pr",
        terminal=False,
        contract=contract,
    )

    result = await node({"ticket_key": "PROJ-1"})

    assert called is False
    assert result["precondition_result"]["action"] == "skip"
    assert result["workflow_transition_count"] == 1


@pytest.mark.asyncio
async def test_guarded_node_without_contract_remains_backward_compatible() -> None:
    async def node(state: dict) -> dict:
        return {**state, "called": True}

    guarded = DeclarativeWorkflowCompiler._guarded_node(
        node,
        "generate_prd",
        terminal=False,
    )

    result = await guarded({"ticket_key": "PROJ-1"})

    assert result["called"] is True
    assert "precondition_result" not in result


def test_rejects_unknown_node_and_unreachable_node() -> None:
    unknown = load_workflow_value(definition_value(steps={"shell": {"next": "__end__"}}))
    with pytest.raises(WorkflowValidationError, match="entry node"):
        DeclarativeWorkflowCompiler(unknown).validate()

    unreachable = load_workflow_value(
        definition_value(
            steps={
                "generate_prd": {"next": "__end__"},
                "generate_spec": {"next": "__end__"},
            }
        )
    )
    with pytest.raises(WorkflowValidationError, match="unreachable"):
        DeclarativeWorkflowCompiler(unreachable).validate()


def test_rejects_unguarded_cycle_but_allows_gate_cycle() -> None:
    unsafe = load_workflow_value(
        definition_value(
            steps={
                "generate_prd": {
                    "route": "route_current_node",
                    "branches": {"again": "generate_prd", "done": "__end__"},
                }
            }
        )
    )
    with pytest.raises(WorkflowValidationError, match="no approved pause"):
        DeclarativeWorkflowCompiler(unsafe).validate()

    guarded = load_workflow_value(
        definition_value(
            steps={
                "generate_prd": {"next": "prd_approval_gate"},
                "prd_approval_gate": {
                    "route": "route_prd_approval",
                    "branches": {
                        "regenerate_prd": "generate_prd",
                        "done": "__end__",
                    },
                },
            }
        )
    )
    DeclarativeWorkflowCompiler(guarded).validate()


def test_label_selection_is_explicit_and_unambiguous() -> None:
    assert selected_workflow_name(["forge:managed", "forge:workflow:short-feature"]) == (
        "short-feature"
    )
    assert selected_workflow_name(["forge:managed"]) is None
    with pytest.raises(ValueError, match="multiple"):
        selected_workflow_name(["forge:workflow:a", "forge:workflow:b"])


@pytest.mark.asyncio
async def test_load_project_workflow_checks_property_metadata_name() -> None:
    jira = AsyncMock()
    jira.get_project_property.return_value = definition_value()

    workflow = await load_project_workflow(jira, "PROJ", "short-feature")

    assert workflow.cache_key.startswith("custom:PROJ:short-feature:1:")
    jira.get_project_property.assert_awaited_once_with("PROJ", "forge.workflow.short-feature")


def test_resume_adopts_revision_and_requires_mapping_for_removed_node() -> None:
    current = DeclarativeWorkflow(load_workflow_value(definition_value(revision=1)), "PROJ")
    state = {
        **current.workflow_metadata(),
        "current_node": "generate_prd",
        "workflow_transition_count": 7,
    }
    updated_value = definition_value(
        revision=2,
        steps={"generate_spec": {"next": "__end__"}},
    )
    updated_value["spec"]["entry"] = "generate_spec"
    updated_value["spec"]["resume"] = {"fromRevisions": {"1": {"generate_prd": "generate_spec"}}}
    updated = DeclarativeWorkflow(load_workflow_value(updated_value), "PROJ")

    migrated = updated.migrate_state(state)

    assert migrated["current_node"] == "generate_spec"
    assert migrated["workflow_revision"] == 2
    assert migrated["workflow_transition_count"] == 7


def test_resume_rejects_same_revision_mutation() -> None:
    original = DeclarativeWorkflow(load_workflow_value(definition_value()), "PROJ")
    changed_value = definition_value(steps={"generate_spec": {"next": "__end__"}})
    changed_value["spec"]["entry"] = "generate_spec"
    changed = DeclarativeWorkflow(load_workflow_value(changed_value), "PROJ")

    with pytest.raises(WorkflowValidationError, match="without incrementing"):
        changed.migrate_state({**original.workflow_metadata(), "current_node": "entry"})


@pytest.mark.asyncio
async def test_worker_resolves_label_selected_workflow() -> None:
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    worker._checkpointer = MagicMock()
    worker._checkpointer.aget = AsyncMock(return_value=None)
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=definition_value())
    jira.close = AsyncMock()

    publisher = AsyncMock()
    publisher.active.return_value = None
    with (
        patch("forge.orchestrator.worker.JiraClient", return_value=jira),
        patch(
            "forge.workflow.declarative.publication.DefinitionPublisher",
            return_value=publisher,
        ),
    ):
        workflow = await worker._resolve_custom_workflow(
            "PROJ-1", ["forge:managed", "forge:workflow:short-feature"]
        )

    assert workflow is not None
    assert workflow.name == "short-feature"
    assert workflow.project_key == "PROJ"


@pytest.mark.asyncio
async def test_worker_keeps_checkpoint_workflow_identity_when_label_is_removed() -> None:
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    worker._checkpointer = MagicMock()
    worker._checkpointer.aget = AsyncMock(
        return_value={
            "channel_values": {
                "workflow_name": "short-feature",
                "workflow_project_key": "PROJ",
            }
        }
    )
    jira = MagicMock()
    jira.get_project_property = AsyncMock(return_value=definition_value())
    jira.close = AsyncMock()

    publisher = AsyncMock()
    publisher.active.return_value = None
    with (
        patch("forge.orchestrator.worker.JiraClient", return_value=jira),
        patch(
            "forge.workflow.declarative.publication.DefinitionPublisher",
            return_value=publisher,
        ),
    ):
        workflow = await worker._resolve_custom_workflow("PROJ-1", [])

    assert workflow is not None
    assert workflow.name == "short-feature"


def test_worker_cache_key_separates_custom_revisions() -> None:
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    worker._compiled_workflows = {}
    worker._checkpointer = None
    first = DeclarativeWorkflow(load_workflow_value(definition_value(revision=1)), "PROJ")
    second = DeclarativeWorkflow(load_workflow_value(definition_value(revision=2)), "PROJ")

    first_compiled = worker._get_compiled_workflow(first)
    second_compiled = worker._get_compiled_workflow(second)

    assert first_compiled is not second_compiled
    assert len(worker._compiled_workflows) == 2


@pytest.mark.asyncio
async def test_cli_publish_validates_and_stores_canonical_json(tmp_path) -> None:
    source = tmp_path / "workflow.yaml"
    source.write_text(
        yaml.safe_dump(builtin_feature_definition().canonical_dict(), sort_keys=False),
        encoding="utf-8",
    )
    publisher = InMemoryDefinitionPublisher("PROJ")

    with patch("forge.workflow.declarative.cli.DefinitionPublisher", return_value=publisher):
        result = await cmd_workflow(
            Namespace(
                workflow_command="publish",
                project_key="proj",
                file=str(source),
                actor="tester",
                reason="contract test",
            )
        )

    assert result == 0
    history = await publisher.history("feature")
    assert len(history) == 1
    assert history[0].canonical_dict()["apiVersion"] == "forge/v1"
    assert history[0].metadata.revision == 1


@pytest.mark.asyncio
async def test_cli_render_does_not_require_jira(tmp_path, capsys) -> None:
    source = tmp_path / "workflow.yaml"
    source.write_text(
        """apiVersion: forge/v1
kind: Workflow
metadata:
  name: short-feature
  revision: 1
spec:
  state: feature
  entry: generate_prd
  steps:
    generate_prd:
      next: __end__
""",
        encoding="utf-8",
    )

    result = await cmd_workflow(
        Namespace(workflow_command="render", file=str(source), format="mermaid")
    )

    assert result == 0
    assert "flowchart TD" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_diff_returns_nonzero_for_unsafe_in_flight_change(tmp_path, capsys) -> None:
    previous = tmp_path / "previous.yaml"
    current = tmp_path / "current.yaml"
    previous.write_text(
        """apiVersion: forge/v1
kind: Workflow
metadata: {name: short-feature, revision: 1}
spec:
  state: feature
  entry: generate_prd
  steps: {generate_prd: {next: __end__}}
""",
        encoding="utf-8",
    )
    current.write_text(
        """apiVersion: forge/v1
kind: Workflow
metadata: {name: short-feature, revision: 2}
spec:
  state: feature
  entry: generate_spec
  steps: {generate_spec: {next: __end__}}
""",
        encoding="utf-8",
    )

    result = await cmd_workflow(
        Namespace(workflow_command="diff", previous=str(previous), current=str(current))
    )

    assert result == 2
    assert '"missing_resume_mappings"' in capsys.readouterr().out
