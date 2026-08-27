"""CLI handlers for validating and managing declarative workflows."""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml  # type: ignore[import-untyped]

from forge.integrations.jira.client import JiraClient
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.loader import load_workflow_file, load_workflow_value
from forge.workflow.declarative.manifest import (
    build_process_manifest,
    compare_process_definitions,
    render_mermaid,
)
from forge.workflow.declarative.models import WORKFLOW_PROPERTY_PREFIX


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

    jira = JiraClient()
    try:
        project_key = args.project_key.upper()
        if action == "publish":
            definition = load_workflow_file(args.file)
            DeclarativeWorkflowCompiler(definition).validate()
            existing = await jira.get_project_property(project_key, definition.property_key)
            if existing is not None:
                try:
                    previous = load_workflow_value(existing)
                except Exception:
                    previous = None  # A valid publication is allowed to repair a broken property.
                if previous is not None:
                    if previous.metadata.name != definition.metadata.name:
                        raise ValueError("existing property has a different workflow name")
                    if previous.digest != definition.digest and (
                        definition.metadata.revision <= previous.metadata.revision
                    ):
                        raise ValueError(
                            "changed workflow content must increment metadata.revision "
                            f"above {previous.metadata.revision}"
                        )
            await jira.set_project_property(
                project_key, definition.property_key, definition.canonical_dict()
            )
            print(
                f"[OK] published {definition.metadata.name} revision "
                f"{definition.metadata.revision} to {project_key}"
            )
            return 0

        if action == "show":
            key = f"{WORKFLOW_PROPERTY_PREFIX}{args.name}"
            value = await jira.get_project_property(project_key, key)
            if value is None:
                raise ValueError(f"workflow '{args.name}' is not defined for {project_key}")
            definition = load_workflow_value(value)
            DeclarativeWorkflowCompiler(definition).validate()
            if args.json:
                print(json.dumps(definition.canonical_dict(), indent=2))
            else:
                print(yaml.safe_dump(definition.canonical_dict(), sort_keys=False).rstrip())
            return 0

        if action == "list":
            keys = await jira.list_project_properties(project_key)
            names = sorted(
                key[len(WORKFLOW_PROPERTY_PREFIX) :]
                for key in keys
                if key.startswith(WORKFLOW_PROPERTY_PREFIX)
            )
            if not names:
                print(f"No custom workflows configured for {project_key}.")
            else:
                for name in names:
                    print(name)
            return 0

        if action == "delete":
            if not args.yes:
                raise ValueError("deleting a workflow requires --yes")
            await jira.delete_project_property(
                project_key, f"{WORKFLOW_PROPERTY_PREFIX}{args.name}"
            )
            print(f"[OK] deleted {args.name} from {project_key}")
            return 0
    except Exception as exc:
        return _print_error(exc)
    finally:
        await jira.close()
    return _print_error(ValueError(f"unknown workflow command: {action}"))
