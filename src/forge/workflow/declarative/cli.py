"""CLI handlers for validating and managing declarative workflows."""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml  # type: ignore[import-untyped]

from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.loader import load_workflow_file
from forge.workflow.declarative.manifest import (
    build_process_manifest,
    compare_process_definitions,
    render_mermaid,
    simulate_process_migration,
)
from forge.workflow.declarative.publication import DefinitionPublisher


def _print_error(exc: Exception) -> int:
    print(f"Error: {exc}", file=sys.stderr)
    return 1


async def cmd_workflow(args: Any) -> int:
    action = args.workflow_command
    if action == "validate":
        try:
            definition = load_workflow_file(args.file)
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
            definition = load_workflow_file(args.file)
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
            previous = load_workflow_file(args.previous)
            current = load_workflow_file(args.current)
            impact = compare_process_definitions(previous, current)
        except Exception as exc:
            return _print_error(exc)
        print(impact.model_dump_json(indent=2))
        return 0 if impact.compatible_for_in_flight else 2

    if action == "simulate-migration":
        try:
            previous = load_workflow_file(args.previous)
            current = load_workflow_file(args.current)
            with open(args.instances, encoding="utf-8") as source:
                instances = json.load(source)
            if not isinstance(instances, list):
                raise ValueError("active instance snapshot must be a JSON array")
            simulation = simulate_process_migration(previous, current, instances)
        except Exception as exc:
            return _print_error(exc)
        print(simulation.model_dump_json(indent=2))
        return 0 if simulation.compatible else 2

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
            definition = await publisher.active(args.name)
            if definition is None:
                raise ValueError(f"workflow '{args.name}' is not defined for {project_key}")
            DeclarativeWorkflowCompiler(definition).validate()
            if getattr(args, "json", False):
                print(json.dumps(definition.canonical_dict(), indent=2))
            else:
                print(yaml.safe_dump(definition.canonical_dict(), sort_keys=False).rstrip())
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
