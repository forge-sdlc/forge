from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import ForgeLabel
from forge.workflow.effect_runtime import JiraClient


async def _set_same_label(client: JiraClient) -> None:
    await client.set_workflow_label("FORGE-1", ForgeLabel.BLOCKED)


@pytest.mark.asyncio
async def test_local_workflow_write_is_journalled_and_deduplicated() -> None:
    provider = MagicMock()
    provider.set_workflow_label = AsyncMock()
    provider.close = AsyncMock()
    with patch("forge.workflow.effect_runtime.ProviderJiraClient", return_value=provider):
        client = JiraClient()
        await _set_same_label(client)
        await _set_same_label(client)

    provider.set_workflow_label.assert_awaited_once_with("FORGE-1", ForgeLabel.BLOCKED.value)


@pytest.mark.asyncio
async def test_failed_required_write_does_not_look_successful() -> None:
    provider = MagicMock()
    provider.set_workflow_label = AsyncMock(side_effect=TimeoutError("provider unavailable"))
    provider.close = AsyncMock()
    with patch("forge.workflow.effect_runtime.ProviderJiraClient", return_value=provider):
        client = JiraClient()
        with pytest.raises(Exception, match="Required effect"):
            await _set_same_label(client)
