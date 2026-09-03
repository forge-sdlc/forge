"""Contract tests for immutable, project-scoped workflow governance."""

from __future__ import annotations

import pytest

from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.loader import load_workflow_value
from forge.workflow.declarative.publication import InMemoryDefinitionPublisher


def definition(revision: int, description: str = ""):
    value = builtin_feature_definition()
    return value.model_copy(
        update={
            "metadata": value.metadata.model_copy(
                update={"revision": revision, "description": description}
            )
        }
    )


@pytest.mark.asyncio
async def test_publish_is_immutable_and_does_not_activate() -> None:
    publisher = InMemoryDefinitionPublisher("proj")
    first = definition(1)
    decision = await publisher.publish(first, actor="alice", reason="initial approval")

    assert decision.action == "publish"
    assert decision.activated is False
    assert await publisher.active("feature") is None

    with pytest.raises(ValueError, match="immutable"):
        await publisher.publish(definition(1, "changed"), actor="alice", reason="mistake")


@pytest.mark.asyncio
async def test_changed_content_must_use_strictly_increasing_revision() -> None:
    publisher = InMemoryDefinitionPublisher("PROJ")
    await publisher.publish(definition(2), actor="alice", reason="approved")

    with pytest.raises(ValueError, match="increment metadata.revision"):
        await publisher.publish(definition(1, "changed"), actor="alice", reason="downgrade")


@pytest.mark.asyncio
async def test_activation_cas_and_rollback_are_audited_without_deleting_history() -> None:
    publisher = InMemoryDefinitionPublisher("proj")
    one = definition(1)
    two = definition(2, "safe change")
    await publisher.publish(one, actor="alice", reason="initial")
    await publisher.publish(two, actor="alice", reason="change")
    activated = await publisher.activate("feature", 2, actor="bob", reason="release")

    with pytest.raises(ValueError, match="concurrently"):
        await publisher.activate("feature", 1, actor="bob", reason="stale", expected_active_digest="wrong")

    rollback = await publisher.rollback(
        "feature", 1, actor="carol", reason="release recovery", expected_active_digest=activated.digest
    )
    assert rollback.action == "rollback"
    assert (await publisher.active("feature")).metadata.revision == 1
    assert [item.action for item in await publisher.decisions("feature")] == [
        "publish",
        "publish",
        "activate",
        "rollback",
    ]
    assert len(await publisher.history("feature")) == 2


@pytest.mark.asyncio
async def test_actor_and_reason_are_required() -> None:
    publisher = InMemoryDefinitionPublisher("proj")
    with pytest.raises(ValueError, match="actor"):
        await publisher.publish(definition(1), actor="", reason="why")
    with pytest.raises(ValueError, match="reason"):
        await publisher.publish(definition(1), actor="alice", reason="")


@pytest.mark.asyncio
async def test_publication_rejects_ungoverned_definition() -> None:
    ungoverned = load_workflow_value(
        {
            "apiVersion": "forge/v1",
            "kind": "Workflow",
            "metadata": {"name": "unsafe", "revision": 1},
            "spec": {
                "state": "feature",
                "entry": "generate_prd",
                "steps": {"generate_prd": {"next": "__end__"}},
            },
        }
    )

    with pytest.raises(ValueError, match="mandatory gate"):
        await InMemoryDefinitionPublisher("proj").publish(
            ungoverned,
            actor="alice",
            reason="should fail",
        )
