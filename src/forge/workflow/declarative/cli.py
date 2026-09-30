"""CLI handlers for validating and managing declarative workflows."""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml  # type: ignore[import-untyped]

from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.composition import resolve_definition, validate_subworkflow
from forge.workflow.declarative.loader import load_workflow_file
from forge.workflow.declarative.manifest import (
    build_process_manifest,
    compare_process_definitions,
    render_mermaid,
    simulate_process_migration,
)
from forge.workflow.declarative.node_publication import NodePublisher
from forge.workflow.declarative.publication import DefinitionPublisher


async def _load_resolved(path: str, project_key: str | None, state_profile: str | None = None):
    source = load_workflow_file(path)
    publisher = DefinitionPublisher(project_key) if project_key else None
    return await resolve_definition(
        source,
        publisher.active if publisher else None,
        state_profile=state_profile,
        node_lookup=NodePublisher(project_key).active if project_key else None,
    )


def _print_error(exc: Exception) -> int:
    print(f"Error: {exc}", file=sys.stderr)
    return 1


async def cmd_workflow(args: Any) -> int:
    action = args.workflow_command
    if action == "validate":
        try:
            definition = await _load_resolved(args.file, getattr(args, "project_key", None))
            if definition.kind == "Subworkflow":
                for state in definition.spec.compatible_states or (definition.spec.state,):
                    profile_definition = await _load_resolved(
                        args.file, getattr(args, "project_key", None), state
                    )
                    validate_subworkflow(profile_definition)
            else:
                DeclarativeWorkflowCompiler(definition).validate()
        except Exception as exc:
            return _print_error(exc)
        print(
            f"[OK] {definition.metadata.name} revision {definition.metadata.revision} "
            f"({definition.digest})"
        )
        if args.json:
            print(json.dumps(definition.canonical_dict(), indent=2))
        return 0

    if action == "render":
        try:
            definition = await _load_resolved(args.file, getattr(args, "project_key", None))
            if definition.kind == "Subworkflow":
                validate_subworkflow(definition)
                if args.format == "json":
                    print(json.dumps(definition.canonical_dict(), indent=2))
                else:
                    lines = ["flowchart TD", f"    __start__([start]) --> {definition.spec.entry}"]
                    for name, step in sorted(definition.spec.steps.items()):
                        lines.append(f'    {name}["{name}"]')
                        if step.next:
                            lines.append(f"    {name} --> {step.next.replace('@exit/', 'exit_')}")
                        elif step.cases:
                            for index, case in enumerate(step.cases):
                                label = json.dumps(
                                    case.when.model_dump(by_alias=True, exclude_none=True),
                                    sort_keys=True,
                                )
                                label = label.replace('"', "&quot;")
                                lines.append(
                                    f"    {name} -->|case {index + 1}: {label}| {case.next.replace('@exit/', 'exit_')}"
                                )
                            lines.append(
                                f"    {name} -->|otherwise| {step.otherwise.replace('@exit/', 'exit_')}"
                            )
                        else:
                            for outcome, target in sorted(step.branches.items()):
                                lines.append(
                                    f"    {name} -->|{outcome}| {target.replace('@exit/', 'exit_')}"
                                )
                    print("\n".join(lines))
                return 0
            manifest = build_process_manifest(definition)
        except Exception as exc:
            return _print_error(exc)
        if args.format == "json":
            print(manifest.model_dump_json(indent=2))
        else:
            print(render_mermaid(manifest))
        return 0

    if action == "diff":
        try:
            previous = await _load_resolved(args.previous, getattr(args, "project_key", None))
            current = await _load_resolved(args.current, getattr(args, "project_key", None))
            impact = compare_process_definitions(previous, current)
        except Exception as exc:
            return _print_error(exc)
        print(impact.model_dump_json(indent=2))
        return 0 if impact.compatible_for_in_flight else 2

    if action == "simulate-migration":
        try:
            previous = await _load_resolved(args.previous, getattr(args, "project_key", None))
            current = await _load_resolved(args.current, getattr(args, "project_key", None))
            with open(args.instances, encoding="utf-8") as source:
                instances = json.load(source)
            if not isinstance(instances, list):
                raise ValueError("active instance snapshot must be a JSON array")
            simulation = simulate_process_migration(previous, current, instances)
        except Exception as exc:
            return _print_error(exc)
        print(simulation.model_dump_json(indent=2))
        return 0 if simulation.compatible else 2

    if action == "catalog":
        from forge.workflow.declarative.catalog import get_state_profile
        from forge.workflow.declarative.predicates import (
            FACT_CONTRACT_VERSION,
            FACTS,
            PROFILE_FACTS,
        )
        from forge.workflow.declarative.router_contracts import ROUTER_CONTRACTS

        profile = get_state_profile(args.state)
        catalog = {
            "state": args.state,
            "nodes": {
                name: {
                    "kind": (
                        "gate"
                        if name in profile.pause_nodes
                        else "station"
                        if name in profile.station_bindings
                        else "operation"
                    ),
                    **(
                        {
                            "stationContract": profile.station_bindings[name][0],
                            "stationContractVersion": profile.station_bindings[name][1],
                        }
                        if name in profile.station_bindings
                        else {}
                    ),
                    "effects": list(profile.effect_policies[name].default),
                    "optionalEffects": sorted(profile.effect_policies[name].optional),
                }
                for name in sorted(profile.nodes)
            },
            "routers": {
                name: {
                    **(
                        {"dynamicTargets": sorted(profile.dynamic_router_targets[name])}
                        if name in profile.dynamic_router_targets
                        else {}
                    ),
                    "outcomesByStep": {
                        step_name: sorted(outcomes)
                        for step_name, (router_name, outcomes) in sorted(
                            ROUTER_CONTRACTS[args.state].items()
                        )
                        if router_name == name
                    },
                }
                for name in sorted(profile.routers)
            },
            "conditionalFacts": {
                "version": FACT_CONTRACT_VERSION,
                "facts": {
                    name: {
                        "type": FACTS[name].value_type.__name__,
                        **(
                            {"values": sorted(FACTS[name].values)}
                            if FACTS[name].values is not None
                            else {}
                        ),
                    }
                    for name in sorted(PROFILE_FACTS[args.state])
                },
            },
            "pauseNodes": sorted(profile.pause_nodes),
            "mandatoryPolicies": sorted(profile.mandatory_policies),
            "observationPolicies": sorted(profile.observation_policy_targets),
        }
        if args.json:
            print(json.dumps(catalog, indent=2))
        else:
            print(yaml.safe_dump(catalog, sort_keys=False).rstrip())
        return 0

    try:
        project_key = args.project_key.upper()
        publisher = DefinitionPublisher(project_key)
        actor = getattr(args, "actor", None) or "forge-cli"
        reason = getattr(args, "reason", None) or f"CLI {action} decision"
        if action == "publish":
            definition = load_workflow_file(args.file)
            decision = await publisher.publish(definition, actor=actor, reason=reason)
            print(
                f"[OK] published {decision.workflow_name} revision {decision.revision} "
                f"to {project_key} (digest {decision.digest})"
            )
            return 0

        if action in {"activate", "rollback"}:
            decision = await getattr(publisher, action)(
                args.name,
                args.revision,
                actor=actor,
                reason=reason,
                expected_active_digest=getattr(args, "expected_active_digest", None),
            )
            verb = "activated" if decision.action == "activate" else "rolled back"
            print(
                f"[OK] {verb} {decision.workflow_name} revision "
                f"{decision.revision} for {project_key}"
            )
            return 0

        if action == "show":
            active_definition = await publisher.active(args.name)
            if active_definition is None:
                raise ValueError(f"workflow '{args.name}' is not defined for {project_key}")
            if active_definition.kind == "Workflow":
                resolved = await resolve_definition(
                    active_definition,
                    publisher.active,
                    node_lookup=NodePublisher(project_key).active,
                )
                DeclarativeWorkflowCompiler(resolved).validate()
            else:
                validate_subworkflow(
                    await resolve_definition(
                        active_definition,
                        publisher.active,
                        node_lookup=NodePublisher(project_key).active,
                    )
                )
            if getattr(args, "json", False):
                print(json.dumps(active_definition.canonical_dict(), indent=2))
            else:
                print(yaml.safe_dump(active_definition.canonical_dict(), sort_keys=False).rstrip())
            return 0

        if action == "list":
            names = await publisher.list_workflows()
            if not names:
                print(f"No custom workflows configured for {project_key}.")
            else:
                for name in names:
                    print(name)
            return 0

        if action == "show-history":
            decisions = await publisher.decisions(args.name)
            if args.json:
                print(json.dumps([item.model_dump(mode="json") for item in decisions], indent=2))
            else:
                for item in decisions:
                    print(
                        f"{item.published_at.isoformat()} {item.action} "
                        f"revision {item.revision} actor={item.actor} reason={item.reason}"
                    )
            return 0

        if action == "delete":
            raise ValueError(
                "destructive workflow deletion is disabled; publish a replacement or use rollback"
            )
    except Exception as exc:
        return _print_error(exc)
    return _print_error(ValueError(f"unknown workflow command: {action}"))
