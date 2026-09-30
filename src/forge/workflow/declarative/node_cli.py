"""CLI for safe project node templates."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from forge.workflow.declarative.loader import LimitedSafeLoader
from forge.workflow.declarative.models import SELECTORS, NodeDefinition
from forge.workflow.declarative.node_publication import NodePublisher, validate_node


def load_node(path: str) -> NodeDefinition:
    raw = Path(path).read_bytes()
    if len(raw) > 32768:
        raise ValueError("node file exceeds 32768 bytes")
    return NodeDefinition.model_validate(yaml.load(raw.decode(), Loader=LimitedSafeLoader))


async def cmd_node(args: Any) -> int:
    try:
        action = args.node_command
        if action == "catalog":
            print(
                json.dumps(
                    {
                        "types": ["decision-v1", "agent-assessment-v1"],
                        "inputs": sorted(SELECTORS),
                        "facts": [
                            "workflow.paused",
                            "workflow.has_error",
                            "ci.status",
                            "pr.merged",
                            "review.revision_requested",
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        if action == "validate":
            definition = load_node(args.file)
            validate_node(definition)
            print(
                f"[OK] {definition.metadata.name} revision {definition.metadata.revision} ({definition.digest})"
            )
            return 0
        publisher = NodePublisher(args.project_key)
        if action == "publish":
            definition = load_node(args.file)
            await publisher.publish(definition)
            print(
                f"[OK] published {definition.metadata.name} revision {definition.metadata.revision} ({definition.digest})"
            )
        elif action in {"activate", "rollback"}:
            definition = await getattr(publisher, action)(
                args.name, args.revision, expected_digest=args.expected_active_digest
            )
            print(
                f"[OK] {action} {definition.metadata.name} revision {definition.metadata.revision}"
            )
        elif action == "show":
            definition = await publisher.active(args.name)
            if definition is None:
                raise ValueError("active node is unavailable")
            print(json.dumps(json.loads(definition.canonical_json()), indent=2))
        elif action == "history":
            definitions = await publisher.history(args.name)
            print(json.dumps([json.loads(item.canonical_json()) for item in definitions], indent=2))
        elif action == "list":
            print("\n".join(await publisher.list_nodes()))
        else:
            raise ValueError("node command is required")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
