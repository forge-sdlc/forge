"""Redis contract for immutable process-definition governance."""

import pytest

from forge.workflow.declarative.builtins import builtin_feature_definition
from forge.workflow.declarative.publication import DefinitionPublisher


@pytest.mark.asyncio
async def test_redis_publication_activation_and_cas(redis_client) -> None:
    publisher = DefinitionPublisher("PROJ", redis_client=redis_client)
    first = builtin_feature_definition()
    second = first.model_copy(
        update={
            "metadata": first.metadata.model_copy(
                update={"revision": 2, "description": "compatible description update"}
            )
        }
    )

    await publisher.publish(first, actor="platform", reason="initial publication")
    await publisher.activate("feature", 1, actor="platform", reason="initial rollout")
    await publisher.publish(second, actor="platform", reason="approved revision")

    with pytest.raises(ValueError, match="concurrently"):
        await publisher.activate(
            "feature",
            2,
            actor="platform",
            reason="stale rollout",
            expected_active_digest="stale",
        )

    await publisher.activate(
        "feature",
        2,
        actor="platform",
        reason="approved rollout",
        expected_active_digest=first.digest,
    )

    assert (await publisher.active("feature")).digest == second.digest
    assert [item.action for item in await publisher.decisions("feature")] == [
        "publish",
        "activate",
        "publish",
        "activate",
    ]
    assert [item.metadata.revision for item in await publisher.history("feature")] == [1, 2]
