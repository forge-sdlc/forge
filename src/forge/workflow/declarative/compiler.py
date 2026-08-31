"""Validate and compile declarative workflow definitions into LangGraph graphs."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.types import Send

from forge.domain import stable_identity
from forge.workflow.declarative.capabilities import (
    KNOWN_EFFECT_CAPABILITIES,
    effect_capability_scope,
)
from forge.workflow.declarative.catalog import get_state_profile
from forge.workflow.declarative.models import MAX_TRANSITIONS, WorkflowDefinition
from forge.workflow.preconditions import NodeContract, project_capabilities, with_preconditions


class WorkflowValidationError(ValueError):
    """A workflow is syntactically valid but unsafe or impossible to compile."""


class DeclarativeWorkflowCompiler:
    def __init__(self, definition: WorkflowDefinition) -> None:
        self.definition = definition
        self.profile = get_state_profile(definition.spec.state)

    def dynamic_targets(self, step: Any) -> frozenset[str]:
        """Return catalog-owned targets, accepting matching legacy metadata."""
        if not step.dynamic_route or not step.route:
            return frozenset()
        targets = self.profile.dynamic_router_targets.get(step.route)
        if not targets:
            raise WorkflowValidationError(
                f"router '{step.route}' is not registered for dynamic routing"
            )
        declared = frozenset(step.dynamic_targets)
        if declared and declared != targets:
            raise WorkflowValidationError(
                f"dynamicTargets for router '{step.route}' are catalog-owned"
            )
        return targets

    def validate(self) -> None:
        spec = self.definition.spec
        steps = spec.steps
        if spec.entry not in steps:
            raise WorkflowValidationError(f"entry node '{spec.entry}' is not declared")

        # The following checks preserve old pinned artifacts. New definitions
        # omit this catalog/governance metadata entirely.
        unknown_policies = set(spec.mandatory_policies) - set(self.profile.mandatory_policies)
        if unknown_policies:
            raise WorkflowValidationError(
                f"unknown mandatory policy '{sorted(unknown_policies)[0]}'"
            )

        observation_policy = spec.observation_policy
        if observation_policy is not None:
            policy_targets = self.profile.observation_policy_targets.get(observation_policy)
            if policy_targets is None:
                raise WorkflowValidationError(f"unknown observation policy '{observation_policy}'")
            missing_policy_targets = policy_targets - set(steps)
            if missing_policy_targets:
                raise WorkflowValidationError(
                    f"observation policy '{observation_policy}' targets undeclared node "
                    f"'{sorted(missing_policy_targets)[0]}'"
                )
            derived_policy = self.profile.observation_policy_for(set(steps))
            if observation_policy != derived_policy:
                raise WorkflowValidationError(
                    f"observation policy '{observation_policy}' is not applicable to this topology"
                )
        legacy_extensions = {"station-behavior", "optional-stations", "routing-branches"}
        unknown_extensions = set(spec.extension_points) - legacy_extensions
        if unknown_extensions:
            raise WorkflowValidationError(
                f"unsupported extension point '{sorted(unknown_extensions)[0]}'"
            )

        unknown_nodes = set(steps) - set(self.profile.nodes)
        if unknown_nodes:
            raise WorkflowValidationError(
                f"node '{sorted(unknown_nodes)[0]}' is not registered for state '{spec.state}'"
            )
        missing_effect_policies = set(steps) - set(self.profile.effect_policies)
        if missing_effect_policies:
            raise WorkflowValidationError(
                f"node '{sorted(missing_effect_policies)[0]}' has no registered effect policy"
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
                else list(self.dynamic_targets(step))
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
            unknown_effects = set(step.allowed_effects or ()) - set(KNOWN_EFFECT_CAPABILITIES)
            if unknown_effects:
                raise WorkflowValidationError(
                    f"step '{node_name}' requests unknown effect capability "
                    f"'{sorted(unknown_effects)[0]}'"
                )
            try:
                self.effective_effects(node_name)
            except ValueError as exc:
                raise WorkflowValidationError(f"step '{node_name}' {exc}") from exc
            binding = self.profile.station_bindings.get(node_name)
            if binding and (step.kind in {"station", "gate"} or step.station_contract):
                declared = (step.station_contract, step.station_contract_version)
                if binding != declared:
                    raise WorkflowValidationError(
                        f"station contract for '{node_name}' must be {binding}, got {declared}"
                    )
            elif step.station_contract:
                raise WorkflowValidationError(
                    f"node '{node_name}' does not support station contract "
                    f"'{step.station_contract}'"
                )
            if step.kind is not None and step.kind != self.profile.node_kind(node_name):
                raise WorkflowValidationError(
                    f"node kind for '{node_name}' is catalog-owned and must be "
                    f"'{self.profile.node_kind(node_name)}'"
                )
            if step.external_entry:
                raise WorkflowValidationError(
                    f"externalEntry is legacy command plumbing and is not valid on '{node_name}'"
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

        incoming: dict[str, set[str]] = {name: set() for name in steps}
        for source, incoming_targets in adjacency.items():
            for target in incoming_targets:
                incoming[target].add(source)
        for node_name, step in steps.items():
            if step.join and len(incoming[node_name]) < 2:
                raise WorkflowValidationError(
                    f"join step '{node_name}' must have at least two incoming transitions"
                )

        # A cycle is safe only if removing pause/bounded-boundary nodes breaks it.
        unguarded = {
            name
            for name, step in steps.items()
            if name not in self.profile.pause_nodes and not step.retry_bound
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

    def validate_for_publication(self) -> None:
        """Apply organizational governance in addition to structural validity."""
        missing_nodes = set(self.profile.mandatory_nodes) - set(self.definition.spec.steps)
        if missing_nodes:
            raise WorkflowValidationError(
                f"publication omits mandatory gate '{sorted(missing_nodes)[0]}'"
            )
        self.validate()
        self._validate_golden_route_contracts()

    def _validate_golden_route_contracts(self) -> None:
        """Keep custom routing within the reviewed golden-path outcome contract."""
        from forge.workflow.declarative.builtins import (
            builtin_bug_definition,
            builtin_feature_definition,
            builtin_task_takeover_definition,
        )

        factories = {
            "feature": builtin_feature_definition,
            "bug": builtin_bug_definition,
            "task_takeover": builtin_task_takeover_definition,
        }
        golden = factories[self.definition.spec.state]()
        for node_name, step in self.definition.spec.steps.items():
            expected = golden.spec.steps.get(node_name)
            if expected is None or not expected.route or step.route != expected.route:
                continue
            expected_outcomes = (
                set(self.dynamic_targets(expected))
                if expected.dynamic_route
                else set(expected.branches)
            )
            declared_outcomes = (
                set(self.dynamic_targets(step)) if step.dynamic_route else set(step.branches)
            )
            missing = expected_outcomes - declared_outcomes
            if missing:
                raise WorkflowValidationError(
                    f"step '{node_name}' omits router outcome '{sorted(missing)[0]}'"
                )
            extra = declared_outcomes - expected_outcomes
            if extra:
                raise WorkflowValidationError(
                    f"step '{node_name}' adds unregistered router outcome '{sorted(extra)[0]}'"
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
                    retry_bound=step.retry_bound,
                    allowed_effects=self.effective_effects(node_name),
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
                graph.add_conditional_edges(
                    node_name,
                    self._guarded_dynamic_router(
                        self.profile.routers[step.route],
                        set(self.dynamic_targets(step)),
                        step.max_concurrency,
                    ),
                )
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

    def effective_effects(self, node_name: str) -> tuple[str, ...]:
        """Resolve catalog-owned authority and an optional supported restriction."""
        step = self.definition.spec.steps[node_name]
        return self.profile.effect_policies[node_name].resolve(step.allowed_effects)

    def effective_observation_policy(self) -> str | None:
        """Return the profile policy implied by this definition's topology."""
        return self.profile.observation_policy_for(set(self.definition.spec.steps))

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
        retry_bound: int | None = None,
        allowed_effects: tuple[str, ...] = (),
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
            attempts = dict(state.get("workflow_node_attempts") or {})
            attempts[node_name] = int(attempts.get(node_name, 0)) + 1
            if retry_bound is not None and attempts[node_name] > retry_bound:
                return {
                    **state,
                    "workflow_transition_count": transitions,
                    "workflow_node_attempts": attempts,
                    "current_node": node_name,
                    "is_blocked": True,
                    "last_error": (
                        f"Declarative step '{node_name}' exceeded retry bound {retry_bound}"
                    ),
                }
            with effect_capability_scope(allowed_effects):
                result = await guarded_func(state)
            if not isinstance(result, dict):
                raise TypeError(f"node '{node_name}' must return a state dictionary")
            # Some legacy nodes route to the shared escalation node by replacing
            # current_node. Preserve the actual failing step so an explicit
            # forge:retry can return there after escalation completes.
            if result.get("current_node") == "escalate_blocked" and node_name != "escalate_blocked":
                result = {**result, "retry_node": node_name}
            if terminal and not any(
                (result.get("last_error"), result.get("is_paused"), result.get("is_blocked"))
            ):
                result = {**result, "current_node": "complete", "is_paused": False}
            target = str(result.get("current_node") or node_name)
            occurred_at = str(result.get("updated_at") or state.get("updated_at") or "")
            transition = {
                "transition_id": stable_identity(
                    "workflow-transition",
                    {
                        "run_id": state.get("thread_id") or state.get("ticket_key"),
                        "count": transitions,
                        "source": node_name,
                        "target": target,
                    },
                ),
                "source": node_name,
                "target": target,
                "occurred_at": occurred_at,
            }
            history = list(state.get("transition_history") or [])
            return {
                **result,
                "capabilities": project_capabilities(result),
                "workflow_transition_count": transitions,
                "workflow_node_attempts": attempts,
                "transition_history": [*history, transition],
            }

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

    @staticmethod
    def _guarded_dynamic_router(
        func: Callable[..., Any], targets: set[str], max_concurrency: int | None
    ) -> Callable[..., Any]:
        async def route(state: dict[str, Any]) -> str | Send | list[Send]:
            if state.get("is_blocked"):
                return "__end__"
            result = func(state)
            if inspect.isawaitable(result):
                result = await result
            values = result if isinstance(result, list) else [result]
            if max_concurrency is not None and len(values) > max_concurrency:
                raise WorkflowValidationError(
                    f"dynamic router emitted {len(values)} branches; maximum is {max_concurrency}"
                )
            for value in values:
                target = value.node if isinstance(value, Send) else value
                if target not in targets:
                    raise WorkflowValidationError(
                        f"dynamic router returned undeclared target {target!r}"
                    )
            if isinstance(result, (str, Send)):
                return result
            if isinstance(result, list) and all(isinstance(value, Send) for value in result):
                return result
            raise WorkflowValidationError("dynamic router must return a target or Send values")

        return route
