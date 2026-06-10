#!/usr/bin/env python3
"""Patch or clear a workflow checkpoint state.

Usage:
    uv run python devtools/patch_checkpoint.py AISOS-358 fork_owner=eshulman2 fork_repo=installer
    uv run python devtools/patch_checkpoint.py AISOS-678 current_node=analyze_bug is_paused=false retry_count=0
    uv run python devtools/patch_checkpoint.py RHWA-527 --clear

The script detects the workflow type from the saved checkpoint (bug vs feature)
and uses the correct compiled graph so BugState fields are not silently dropped.
Use --clear to delete the checkpoint entirely so the next event starts a fresh workflow.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


async def patch(ticket_key: str, patches: dict) -> None:
    from forge.models.workflow import TicketType
    from forge.orchestrator.checkpointer import get_checkpointer
    from forge.workflow.registry import create_default_router

    checkpointer = await get_checkpointer()
    router = create_default_router()

    # Detect workflow type from saved state so we use the right graph
    # (using the wrong graph silently drops fields not in that state schema)
    config = {"configurable": {"thread_id": ticket_key}}

    # aget() returns the Checkpoint dict directly (not a CheckpointTuple).
    # Access via dict keys, matching the pattern in worker._find_workflow_by_state.
    detected_type = TicketType.FEATURE
    try:
        raw = await checkpointer.aget(config)
        if raw and isinstance(raw, dict):
            channel_values = raw.get("channel_values", {})
            saved_type = channel_values.get("ticket_type", "")
            if str(saved_type).lower() == "bug":
                detected_type = TicketType.BUG
    except Exception:
        pass

    workflow_instance = router.resolve(ticket_type=detected_type, labels=[], event={})
    graph = workflow_instance.build_graph()
    compiled = graph.compile(checkpointer=checkpointer)

    state = await compiled.aget_state(config)
    if not state or not state.values:
        print(f"No checkpoint found for {ticket_key}")
        return

    type_label = "Bug" if detected_type == TicketType.BUG else "Feature"
    print(f"Detected workflow type: {type_label}")
    print(f"Current state fields relevant to patch:")
    for k in patches:
        print(f"  {k}: {state.values.get(k)!r}")

    await compiled.aupdate_state(config, patches)

    # Verify
    updated = await compiled.aget_state(config)
    print(f"\nPatched successfully:")
    for k, v in patches.items():
        print(f"  {k}: {updated.values.get(k)!r}")


async def clear(ticket_key: str) -> None:
    from forge.orchestrator.checkpointer import clear_checkpoint

    cleared = await clear_checkpoint(ticket_key)
    if cleared:
        print(f"Checkpoint cleared for {ticket_key} — next event starts a fresh workflow")
    else:
        print(f"No checkpoint found for {ticket_key}")


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: patch_checkpoint.py <ticket_key> <field=value> [field=value ...]")
        print("       patch_checkpoint.py <ticket_key> --clear")
        print("Example: patch_checkpoint.py AISOS-358 fork_owner=eshulman2 fork_repo=installer")
        sys.exit(1)

    ticket_key = sys.argv[1]

    if "--clear" in sys.argv[2:]:
        asyncio.run(clear(ticket_key))
        return

    patches: dict = {}

    for arg in sys.argv[2:]:
        if "=" not in arg:
            print(f"Invalid argument (expected field=value): {arg}")
            sys.exit(1)
        field, _, raw_value = arg.partition("=")
        # Try to parse as JSON for booleans/numbers/null, fall back to string
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        patches[field] = value

    asyncio.run(patch(ticket_key, patches))


if __name__ == "__main__":
    main()
