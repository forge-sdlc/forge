"""Validate and compile declarative workflow definitions into LangGraph graphs."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, StateGraph

from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.models import MAX_TRANSITIONS, WorkflowDefinition
from forge.workflow.preconditions import NodeContract, with_preconditions


class WorkflowValidationError(ValueError):
    """A workflow is syntactically valid but unsafe or impossible to compile."""


class DeclarativeWorkflowCompiler:
    def __init__(self, definition: WorkflowDefinition) -> None:
        self.definition = definition
        self.profile = get_state_profile(definition.spec.state)

    def validate(self) -> None:
        spec = self.definition.spec
        steps = spec.steps
        if spec.entry not in steps:
            raise WorkflowValidationError(f"entry node '{spec.entry}' is not declared")

        unknown_nodes = set(steps) - set(self.profile.nodes)
        if unknown_nodes:
            raise WorkflowValidationError(
                f"node '{sorted(unknown_nodes)[0]}' is not registered for state '{spec.state}'"
            )

        adjacency: dict[str, set[str]] = {name: set() for name in steps}
        has_terminal = False
        for node_name, step in steps.items():
            if step.route and step.route not in self.profile.routers:
                raise WorkflowValidationError(
                    f"router '{step.route}' on '{node_name}' is not registered for state "
                    f"'{spec.state}'"
                )
            targets = (
                [step.next]
                if step.next
                else list(step.dynamic_targets)
                if step.dynamic_route
                else list(step.branches.values())
            )
            for target in targets:
                if target == "__end__":
                    has_terminal = True
                elif target not in steps:
                    raise WorkflowValidationError(
                        f"step '{node_name}' targets undeclared node '{target}'"
                    )
                else:
                    adjacency[node_name].add(target)
            missing_policies = set(self.definition.spec.mandatory_policies) - set(
                step.required_policies
            )
            if missing_policies:
                raise WorkflowValidationError(
                    f"step '{node_name}' omits mandatory policy '{sorted(missing_policies)[0]}'"
                )
            if step.kind == "station":
                binding = self.profile.station_bindings.get(node_name)
                declared = (step.station_contract, step.station_contract_version)
                if binding != declared:
                    raise WorkflowValidationError(
                        f"station contract for '{node_name}' is not registered: {declared}"
                    )

        if not has_terminal:
            raise WorkflowValidationError("at least one path must target '__end__'")

        reachable: set[str] = set()
        stack = [spec.entry]
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            stack.extend(adjacency[node])
        unreachable = set(steps) - reachable
        if unreachable:
            raise WorkflowValidationError(f"unreachable node '{sorted(unreachable)[0]}'")

        # A cycle is safe only if removing pause/bounded-boundary nodes breaks it.
        unguarded = {
            name
            for name, step in steps.items()
            if name not in self.profile.pause_nodes and step.kind != "gate" and not step.retry_bound
        }
        colors: dict[str, int] = {}

        def visit(node: str) -> None:
            colors[node] = 1
            for target in adjacency[node]:
                if target not in unguarded:
                    continue
                if colors.get(target) == 1:
                    raise WorkflowValidationError(
                        f"cycle through '{target}' has no approved pause or bounded-retry boundary"
                    )
                if colors.get(target, 0) == 0:
                    visit(target)
            colors[node] = 2

        for node in sorted(unguarded):
            if colors.get(node, 0) == 0:
                visit(node)

        for old_revision, mappings in spec.resume.from_revisions.items():
            if old_revision >= self.definition.metadata.revision:
                raise WorkflowValidationError("resume source revisions must be older than revision")
            for target in mappings.values():
                if target not in steps:
                    raise WorkflowValidationError(
                        f"resume mapping targets undeclared node '{target}'"
                    )

    def build_graph(self) -> StateGraph[Any]:
        self.validate()
        graph: StateGraph[Any] = StateGraph(self.profile.schema)
        graph.add_node("_forge_entry", lambda state: state)
        for node_name, step in self.definition.spec.steps.items():
            graph.add_node(
                node_name,
                self._guarded_node(
                    self.profile.nodes[node_name],
                    node_name,
                    terminal=step.next == "__end__",
                    contract=self.profile.contracts.get(node_name),
                ),
            )
        graph.set_entry_point("_forge_entry")
        graph.add_conditional_edges(
            "_forge_entry",
            self._entry_route(),
            {name: name for name in self.definition.spec.steps},
        )

        for node_name, step in self.definition.spec.steps.items():
            if step.next:
                target = END if step.next == "__end__" else step.next
                graph.add_conditional_edges(
                    node_name,
                    self._fixed_route(step.next),
                    {step.next: target, "__end__": END},
                )
                continue

            assert step.route is not None
            if step.dynamic_route:
                graph.add_conditional_edges(node_name, self.profile.routers[step.route])
                continue
            branches: dict[Any, str] = {
                outcome: END if target == "__end__" else target
                for outcome, target in step.branches.items()
            }
            branches.setdefault("__end__", END)
            graph.add_conditional_edges(
                node_name,
                self._guarded_router(self.profile.routers[step.route], set(branches)),
                branches,
            )
        return graph

    def _entry_route(self) -> Callable[[dict[str, Any]], str]:
        def route(state: dict[str, Any]) -> str:
            current = state.get("current_node")
            if current in self.definition.spec.steps:
                return str(current)
            return self.definition.spec.entry

        return route

    @staticmethod
    def _guarded_node(
        func: Callable[..., Any],
        node_name: str,
        *,
        terminal: bool,
        contract: NodeContract | None = None,
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        guarded_func = with_preconditions(func, contract, node_name=node_name)

        async def run(state: dict[str, Any]) -> dict[str, Any]:
            transitions = int(state.get("workflow_transition_count", 0)) + 1
            if transitions > MAX_TRANSITIONS:
                return {
                    **state,
                    "workflow_transition_count": transitions,
                    "current_node": node_name,
                    "is_blocked": True,
                    "last_error": f"Declarative workflow exceeded {MAX_TRANSITIONS} transitions",
                }
            result = await guarded_func(state)
            if not isinstance(result, dict):
                raise TypeError(f"node '{node_name}' must return a state dictionary")
            if terminal and not any(
                (result.get("last_error"), result.get("is_paused"), result.get("is_blocked"))
            ):
                result = {**result, "current_node": "complete", "is_paused": False}
            return {**result, "workflow_transition_count": transitions}

        run.__name__ = f"declarative_{node_name}"
        return run

    @staticmethod
    def _fixed_route(target: str) -> Callable[[dict[str, Any]], str]:
        def route(state: dict[str, Any]) -> str:
            return "__end__" if state.get("is_blocked") else target

        return route

    @staticmethod
    def _guarded_router(func: Callable[..., Any], outcomes: set[str]) -> Callable[..., Any]:
        async def route(state: dict[str, Any]) -> str:
            if state.get("is_blocked"):
                return "__end__"
            result = func(state)
            if inspect.isawaitable(result):
                result = await result
            normalized = "__end__" if result == END else result
            if not isinstance(normalized, str) or normalized not in outcomes:
                raise WorkflowValidationError(f"router returned undeclared outcome {normalized!r}")
            return normalized

        return route
