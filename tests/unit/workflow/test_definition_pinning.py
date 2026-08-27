from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.orchestrator.worker import OrchestratorWorker
from forge.workflow.checkpoint_migration import migrate_unpinned_checkpoint
from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.models import WorkflowMetadata
from forge.workflow.declarative.resolver import load_project_workflow
from forge.workflow.declarative.workflow import DeclarativeWorkflow


def definition(revision: int = 1, description: str = "") -> dict:
    source = builtin_feature_definition()
    metadata = WorkflowMetadata(name="pinned", revision=revision, description=description)
    return source.model_copy(update={"metadata": metadata}).canonical_dict()


def test_new_state_contains_complete_immutable_identity() -> None:
    workflow = DeclarativeWorkflow(load_workflow_value(definition()), "PROJ")

    state = workflow.create_initial_state("PROJ-1")

    assert state["workflow_name"] == "pinned"
    assert state["workflow_revision"] == 1
    assert state["workflow_definition_revision"] == 1
    assert state["workflow_digest"] == state["workflow_definition_digest"]
    assert state["workflow_definition"] == workflow.definition.canonical_dict()
    assert state["workflow_pin_status"] == "pinned"


@pytest.mark.asyncio
async def test_pinned_resolution_uses_checkpoint_artifact_without_jira_property() -> None:
    pinned = load_workflow_value(definition())
    jira = MagicMock()
    jira.get_project_property = AsyncMock(
        return_value=definition(revision=2, description="new active definition")
    )

    workflow = await load_project_workflow(
        jira,
        "PROJ",
        "pinned",
        pinned_revision=pinned.metadata.revision,
        pinned_digest=pinned.digest,
        pinned_definition=pinned.canonical_dict(),
    )

    assert workflow.definition.digest == pinned.digest
    jira.get_project_property.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_resolves_pinned_checkpoint_without_loading_active_property() -> None:
    pinned = load_workflow_value(definition())
    worker = OrchestratorWorker.__new__(OrchestratorWorker)
    worker._checkpointer = MagicMock()
    worker._checkpointer.aget = AsyncMock(
        return_value={"channel_values": {**DeclarativeWorkflow(pinned, "PROJ").workflow_metadata()}}
    )

    with patch("forge.orchestrator.worker.JiraClient") as jira_client:
        workflow = await worker._resolve_custom_workflow("PROJ-1", [])

    assert workflow is not None
    assert workflow.definition.digest == pinned.digest
    jira_client.assert_not_called()


def test_pinned_state_rejects_digest_mismatch() -> None:
    workflow = DeclarativeWorkflow(load_workflow_value(definition()), "PROJ")
    state = {**workflow.workflow_metadata(), "workflow_definition_digest": "sha256:wrong"}

    with pytest.raises(Exception, match="conflicting workflow identities"):
        workflow.validate_pinned_state(state)


def test_legacy_state_requires_explicit_checkpoint_migration() -> None:
    workflow = DeclarativeWorkflow(load_workflow_value(definition()), "PROJ")
    legacy = {"workflow_name": "pinned", "current_node": "generate_prd"}

    assert workflow.pin_status(legacy) == "legacy_unpinned"
    dry_run = migrate_unpinned_checkpoint(legacy, workflow.definition, apply=False)
    assert dry_run.compatible
    assert not dry_run.applied
    assert dry_run.migrated_state is None

    pinned = migrate_unpinned_checkpoint(
        legacy, workflow.definition, apply=True
    ).migrated_state
    assert pinned is not None
    assert pinned["workflow_pin_status"] == "phase8_migrated"
    assert pinned["workflow_digest"] == workflow.definition.digest
