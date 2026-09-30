"""Offline runner must enforce and save production structured results."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.integrations.agents.structured_outputs import ArtifactDocument


@pytest.fixture
def runner_module():
    path = Path(__file__).parents[3] / "devtools" / "test-skill" / "run.py"
    spec = importlib.util.spec_from_file_location("forge_skill_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_runner_enforces_production_schema_and_exports_document(
    runner_module, tmp_path, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.setattr(runner_module, "LCChatAnthropic", MagicMock())
    agent = MagicMock()
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [],
            "structured_response": {"content": "# Complete PRD", "repositories": ["owner/repo"]},
        }
    )
    create = MagicMock(return_value=agent)
    monkeypatch.setattr(runner_module, "create_deep_agent", create)
    result = await runner_module.run_agent_deepagents(
        "system", "task", tmp_path, {}, "generate-prd", "default"
    )
    assert create.call_args.kwargs["response_format"].schema_spec.schema is ArtifactDocument
    assert result["final_text"] == "# Complete PRD"
    assert result["structured_response"]["repositories"] == ["owner/repo"]


@pytest.mark.asyncio
async def test_runner_rejects_unstructured_success(runner_module, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.setattr(runner_module, "LCChatAnthropic", MagicMock())
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(runner_module, "create_deep_agent", MagicMock(return_value=agent))
    with pytest.raises(ValueError, match="structured response"):
        await runner_module.run_agent_deepagents(
            "system", "task", tmp_path, {}, "generate-spec", "default"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skill", "response"),
    [
        ("triage-bug", {"sufficient": True, "missing_fields": []}),
        (
            "triage-automated-review",
            {"verdict": "satisfied", "blocking_feedback": "", "reason": "Accepted"},
        ),
        ("triage-proposal-review-threads", {"decisions": []}),
    ],
)
async def test_non_planning_stage_schemas_are_enforced(
    runner_module, tmp_path, monkeypatch, skill, response
):
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.setattr(runner_module, "LCChatAnthropic", MagicMock())
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [], "structured_response": response})
    monkeypatch.setattr(runner_module, "create_deep_agent", MagicMock(return_value=agent))
    result = await runner_module.run_agent_deepagents(
        "system", "task", tmp_path, {}, skill, "default"
    )
    assert result["structured_response"] == response
