"""End-to-end smoke tests for the current pluggable workflow architecture."""

import asyncio
from unittest.mock import patch

from forge.models.workflow import TicketType
from forge.workflow.declarative.builtins import FeatureGoldenWorkflow
from forge.workflow.registry import create_default_router


def _generate_prd(state: dict) -> dict:
    """Deterministic external-agent boundary for the workflow smoke test."""
    return {
        **state,
        "prd_content": "# PRD\n\nA deterministic generated product requirement.",
        "current_node": "generate_prd",
        "last_error": None,
    }


def test_feature_workflow_routes_generates_and_pauses() -> None:
    """Exercise registry -> graph -> node -> approval gate."""
    router = create_default_router()
    workflow = router.resolve(TicketType.FEATURE, ["forge:managed"], {})

    assert isinstance(workflow, FeatureGoldenWorkflow)

    with patch("forge.workflow.nodes.generate_prd", _generate_prd):
        graph = workflow.build_graph().compile()
        state = workflow.create_initial_state("TEST-123")
        result = asyncio.run(graph.ainvoke(state))

    assert result["prd_content"].startswith("# PRD")
    assert result["current_node"] == "prd_approval_gate"
    assert result["is_paused"] is True


def test_default_router_exposes_all_builtin_workflows() -> None:
    """Catch registration regressions that make a workflow unreachable."""
    registered = {item["name"] for item in create_default_router().list_workflows()}

    assert registered == {"feature", "bug", "task_takeover"}
