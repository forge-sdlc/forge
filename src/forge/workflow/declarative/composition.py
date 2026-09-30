"""Resolve reusable definitions into the flat, checkpoint-safe process graph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from forge.workflow.declarative.models import MAX_STEPS, WorkflowDefinition, WorkflowStep

ProjectLookup = Callable[[str], Awaitable[WorkflowDefinition | None]]


def validate_subworkflow(definition: WorkflowDefinition) -> None:
    """Check fragment authority without requiring a standalone terminal graph."""
    from forge.workflow.declarative.catalog import get_state_profile

    if definition.kind != "Subworkflow":
        raise ValueError("expected a subworkflow")
    if definition.spec.includes:
        raise ValueError("publish nested subworkflows after their includes are resolved")
    if definition.spec.entry not in definition.spec.steps:
        raise ValueError("subworkflow entry is not declared")
    if not definition.spec.steps:
        raise ValueError("subworkflow requires steps")
    for state in definition.spec.compatible_states or (definition.spec.state,):
        profile = get_state_profile(state)
        for name, step in definition.spec.steps.items():
            if name not in profile.nodes:
                raise ValueError(f"node '{name}' is not registered for state '{state}'")
            if step.route and step.route not in profile.routers:
                raise ValueError(f"router '{step.route}' is not registered for state '{state}'")
            if step.route and not step.dynamic_route:
                from forge.workflow.declarative.router_contracts import validate_router_outcomes

                validate_router_outcomes(state, name, step.route, set(step.branches))
            if step.cases:
                from forge.workflow.declarative.predicates import validate_predicate

                if name in profile.mandatory_nodes or name.endswith("_gate"):
                    raise ValueError(f"protected gate '{name}' requires a trusted router")
                for case in step.cases:
                    validate_predicate(case.when, state)
            profile.effect_policies[name].resolve(step.allowed_effects)
            targets = (
                [step.next]
                if step.next
                else [*(case.next for case in step.cases), step.otherwise]
                if step.cases
                else step.branches.values()
            )
            for target in targets:
                if (
                    target != "__end__"
                    and not target.startswith("@exit/")
                    and target not in definition.spec.steps
                ):
                    raise ValueError(f"subworkflow step '{name}' has undeclared target '{target}'")
                if target.startswith("@exit/") and not target[6:]:
                    raise ValueError("subworkflow exit name is required")


async def resolve_definition(
    definition: WorkflowDefinition,
    project_lookup: ProjectLookup | None = None,
    *,
    state_profile: str | None = None,
) -> WorkflowDefinition:
    """Pin all active dependencies and replace includes with their trusted steps."""

    async def expand(
        source: WorkflowDefinition, chain: tuple[str, ...], profile: str
    ) -> tuple[dict[str, WorkflowStep], tuple[str, ...]]:
        steps = dict(source.spec.steps)
        dependencies: list[str] = []
        seen: set[tuple[str, str]] = set()
        for include in source.spec.includes:
            key = (include.source, include.name)
            if key in seen:
                raise ValueError(
                    f"definition '{include.name}' may be included only once per caller"
                )
            seen.add(key)
            identity = f"{include.source}:{include.name}"
            if identity in chain:
                raise ValueError(f"workflow dependency cycle: {' -> '.join((*chain, identity))}")
            if include.source == "builtin":
                from forge.workflow.declarative.builtins import load_builtin_source

                child = load_builtin_source(include.name)
            else:
                if project_lookup is None:
                    raise ValueError(
                        f"project dependency '{include.name}' needs a project resolver"
                    )
                child = await project_lookup(include.name)
                if child is None:
                    raise ValueError(f"project dependency '{include.name}' is not active")
            if child.metadata.name != include.name:
                raise ValueError(f"dependency '{include.name}' has a mismatched name")
            allowed = child.spec.compatible_states or (child.spec.state,)
            if profile not in allowed:
                raise ValueError(f"dependency '{include.name}' does not support '{profile}'")
            child_steps, nested = await expand(child, (*chain, identity), profile)
            dependencies.append(f"{identity}:{child.metadata.revision}:{child.digest}")
            dependencies.extend(nested)
            if child.kind == "Subworkflow":
                declared_exits = {
                    target[6:]
                    for step in child_steps.values()
                    for target in (
                        [step.next]
                        if step.next
                        else [*(case.next for case in step.cases), step.otherwise]
                        if step.cases
                        else step.branches.values()
                    )
                    if target and target.startswith("@exit/")
                }
                if set(include.exits) != declared_exits or include.return_to is not None:
                    raise ValueError(
                        f"subworkflow '{include.name}' requires exactly these exits: "
                        f"{', '.join(sorted(declared_exits))}"
                    )
                child_steps = {
                    name: _replace_targets(
                        step, {f"@exit/{port}": target for port, target in include.exits.items()}
                    )
                    for name, step in child_steps.items()
                }
            else:
                if include.exits or include.return_to is None:
                    raise ValueError(f"workflow '{include.name}' requires returnTo and no exits")
                terminals = [name for name, step in child_steps.items() if step.next == "__end__"]
                if not terminals:
                    raise ValueError(f"workflow '{include.name}' has no normal completion")
                child_steps = {
                    name: _replace_targets(step, {"__end__": include.return_to})
                    if name in terminals
                    else step
                    for name, step in child_steps.items()
                }
            collision = set(steps) & set(child_steps)
            if collision:
                raise ValueError(f"included node '{sorted(collision)[0]}' collides with caller")
            steps.update(child_steps)
            if len(steps) > MAX_STEPS:
                raise ValueError(f"expanded workflow exceeds {MAX_STEPS} steps")
        return steps, tuple(dependencies)

    if definition.spec.resolved_dependencies:
        if definition.spec.includes:
            raise ValueError("resolved artifact cannot contain includes")
        return definition
    steps, dependencies = await expand(
        definition, (f"root:{definition.metadata.name}",), state_profile or definition.spec.state
    )
    resolved = definition.model_copy(
        update={
            "spec": definition.spec.model_copy(
                update={
                    "steps": steps,
                    "includes": (),
                    "resolved_dependencies": dependencies,
                }
            )
        }
    )
    resolved.validate_property_size()
    return resolved


def _replace_targets(step: WorkflowStep, replacements: dict[str, str]) -> WorkflowStep:
    return step.model_copy(
        update={
            "next": replacements.get(step.next, step.next) if step.next else None,
            "branches": {
                outcome: replacements.get(target, target)
                for outcome, target in step.branches.items()
            },
            "cases": tuple(
                case.model_copy(update={"next": replacements.get(case.next, case.next)})
                for case in step.cases
            ),
            "otherwise": replacements.get(step.otherwise, step.otherwise),
        }
    )
