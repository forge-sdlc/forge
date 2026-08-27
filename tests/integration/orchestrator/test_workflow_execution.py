"""Integration coverage for the declarative workflow runtime."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from forge.models.workflow import TicketType
from forge.workflow.declarative.builtins import builtin_definitions
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.gates.prd_approval import route_prd_approval
from forge.workflow.registry import create_default_router


@pytest.fixture
def temp_checkpoint_db() -> Path:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as file:
        yield Path(file.name)


@pytest.mark.parametrize(
    ("ticket_type", "workflow_name", "entry"),
    (
        (TicketType.FEATURE, "feature", "generate_prd"),
        (TicketType.BUG, "bug", "triage_check"),
        (TicketType.TASK, "task_takeover", "triage_check"),
    ),
)
def test_ticket_type_selects_independent_golden_path(
    ticket_type: TicketType, workflow_name: str, entry: str
) -> None:
    selected = create_default_router().resolve(ticket_type, ["forge:managed"], {})

    assert selected is not None
    assert selected.name == workflow_name
    assert selected.definition.spec.entry == entry


@pytest.mark.parametrize("definition", builtin_definitions(), ids=lambda item: item.metadata.name)
def test_builtin_graph_compiles_and_contains_declared_steps(definition) -> None:
    graph = DeclarativeWorkflowCompiler(definition).build_graph()

    assert set(definition.spec.steps).issubset(graph.nodes)
    assert graph.compile() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", builtin_definitions(), ids=lambda item: item.metadata.name)
async def test_builtin_graph_compiles_with_durable_checkpointer(
    definition, temp_checkpoint_db: Path
) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(temp_checkpoint_db)) as checkpointer:
        compiled = (
            DeclarativeWorkflowCompiler(definition).build_graph().compile(checkpointer=checkpointer)
        )

        assert compiled.checkpointer is checkpointer


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        (
            {"is_paused": False, "revision_requested": False, "prd_content": "approved"},
            "generate_spec",
        ),
        (
            {
                "is_paused": False,
                "revision_requested": True,
                "feedback_comment": "revise",
                "prd_content": "revise",
            },
            "regenerate_prd",
        ),
        (
            {"is_paused": True, "revision_requested": False, "prd_content": "waiting"},
            "__end__",
        ),
    ),
)
def test_prd_gate_routes_current_process_state(state: dict, expected: str) -> None:
    state["ticket_key"] = "TEST-123"
    assert route_prd_approval(state) == expected
