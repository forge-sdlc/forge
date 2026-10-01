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
    unsupported = [
        name
        for name, present in (
            ("observationPolicy", definition.spec.observation_policy is not None),
            ("mandatoryPolicies", bool(definition.spec.mandatory_policies)),
            ("extensionPoints", bool(definition.spec.extension_points)),
            ("resume.fromRevisions", bool(definition.spec.resume.from_revisions)),
        )
        if present
    ]
    if unsupported:
        raise ValueError(
            "subworkflow fields are not applied to the caller: " + ", ".join(unsupported)
        )
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
            profile.effect_policies[name].resolve(step.allowed_effects)
            targets = [step.next] if step.next else step.branches.values()
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
        included_by: dict[str, str] = {}
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
            child: WorkflowDefinition | None
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
                    raise ValueError(
                        f"project dependency '{include.name}' is not active; "
                        "publish and activate it before including it"
                    )
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
                    for target in ([step.next] if step.next else step.branches.values())
                    if target and target.startswith("@exit/")
                }
                if (
                    set(include.exits) != declared_exits
                    or include.return_to is not None
                    or include.return_from
                ):
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
                if include.exits or include.return_to is None or not include.return_from:
                    raise ValueError(
                        f"workflow '{include.name}' requires returnTo and explicit returnFrom edges"
                    )
                child_steps = _bind_workflow_returns(
                    child_steps, include.return_from, include.return_to
                )
            collision = set(steps) & set(child_steps)
            if collision:
                node = sorted(collision)[0]
                if node in included_by:
                    raise ValueError(
                        f"included node '{node}' appears through both "
                        f"'{included_by[node]}' and '{identity}'"
                    )
                raise ValueError(f"included node '{node}' collides with caller")
            steps.update(child_steps)
            included_by.update(dict.fromkeys(child_steps, identity))
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
        }
    )


def _bind_workflow_returns(
    steps: dict[str, WorkflowStep], selectors: tuple[str, ...], return_to: str
) -> dict[str, WorkflowStep]:
    """Bind only the child terminal edges explicitly selected by the caller."""
    if len(selectors) != len(set(selectors)):
        raise ValueError("returnFrom contains duplicate edges")
    bound = dict(steps)
    for selector in selectors:
        node, separator, outcome = selector.partition(":")
        step = bound.get(node)
        if step is None:
            raise ValueError(f"returnFrom edge '{selector}' has no declared step")
        if separator:
            if not outcome or step.branches.get(outcome) != "__end__":
                raise ValueError(f"returnFrom edge '{selector}' is not a terminal branch")
            bound[node] = step.model_copy(
                update={"branches": {**step.branches, outcome: return_to}}
            )
        else:
            if step.next != "__end__":
                raise ValueError(f"returnFrom edge '{selector}' is not a fixed terminal")
            bound[node] = step.model_copy(update={"next": return_to})
    return bound
