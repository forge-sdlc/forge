"""Unit tests for Task Takeover workflow state and graph structure."""

from typing import Any, cast

import pytest
from langgraph.graph import END, StateGraph

from forge.models.workflow import TicketType
from forge.workflow.task_takeover.graph import (
    _route_after_triage_check,
    build_task_takeover_graph,
    route_entry,
)
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
    create_initial_task_takeover_state,
)


def _task_state(**overrides: Any) -> TaskTakeoverState:
    base = {
        "ticket_key": "TASK-1",
        "ticket_type": TicketType.TASK,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "triage_passed": False,
        "triage_missing_fields": [],
        "plan_content": None,
    }
    return cast(TaskTakeoverState, {**base, **overrides})


class TestTaskTakeoverState:
    """Test TaskTakeoverState definition and initial state creation."""

    def test_state_fields(self) -> None:
        """Verify TaskTakeoverState has required fields."""
        # Simple instantiation to verify type definition
        state = TaskTakeoverState(
            triage_passed=True,
            triage_missing_fields=["steps"],
            plan_content="Takeover plan",
        )
        assert state["triage_passed"] is True
        assert state["triage_missing_fields"] == ["steps"]
        assert state["plan_content"] == "Takeover plan"

    def test_create_initial_state_defaults(self) -> None:
        """create_initial_task_takeover_state sets default values appropriately."""
        state = create_initial_task_takeover_state("TASK-1")
        assert state["ticket_key"] == "TASK-1"
        assert state["ticket_type"] == TicketType.TASK
        assert state["triage_passed"] is False
        assert state["triage_missing_fields"] == []
        assert state["plan_content"] is None
        assert state["current_node"] == "start"


class TestRouteEntry:
    """route_entry maps current_node values to correct resume targets."""

    @pytest.mark.parametrize(
        "node,expected",
        [
            ("triage_check", "triage_check"),
            ("triage_gate", "triage_gate"),
            ("generate_plan", "generate_plan"),
            ("task_plan_approval_gate", "task_plan_approval_gate"),
            ("escalate_blocked", "escalate_blocked"),
            ("complete", END),
        ],
    )
    def test_route_entry_mapping(self, node: str, expected: str) -> None:
        """route_entry maps each current_node to the correct resume target."""
        state = _task_state(current_node=node)
        assert route_entry(state) == expected

    def test_new_task_routes_to_triage(self) -> None:
        """A fresh task takeover ticket with no current_node starts at triage_check."""
        state = create_initial_task_takeover_state(ticket_key="TASK-1")
        assert route_entry(state) == "triage_check"

    def test_unknown_node_routes_to_triage(self) -> None:
        """An unrecognized current_node value restarts from triage_check."""
        state = _task_state(current_node="unrecognized_node")
        assert route_entry(state) == "triage_check"


class TestTriageCheckRouting:
    """_route_after_triage_check transitions correctly."""

    @pytest.mark.parametrize(
        "current_node,expected",
        [
            ("triage_check", "triage_check"),
            ("analyze_bug", "generate_plan"),
            ("triage_gate", "triage_gate"),
            ("escalate_blocked", "escalate_blocked"),
            ("unknown_node", "triage_gate"),
        ],
    )
    def test_route_after_triage_check(self, current_node: str, expected: str) -> None:
        """_route_after_triage_check maps triage results to task takeover nodes."""
        state = _task_state(current_node=current_node)
        assert _route_after_triage_check(state) == expected


class TestTaskTakeoverGraph:
    """Test StateGraph compilation and logic."""

    def test_build_task_takeover_graph(self) -> None:
        """build_task_takeover_graph returns a compiled StateGraph."""
        graph = build_task_takeover_graph()
        assert isinstance(graph, StateGraph)

        # Compile the graph to verify correctness
        compiled_graph = graph.compile()
        assert compiled_graph is not None
