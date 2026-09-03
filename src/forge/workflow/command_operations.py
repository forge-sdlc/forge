"""Exceptional operations invoked by commands outside lifecycle graph topology."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.domain import WorkflowCommand, WorkflowCommandType
from forge.workflow.nodes.rebase import rebase_pr


async def execute_command_operation(
    command: WorkflowCommand, state: Mapping[str, Any]
) -> dict[str, Any]:
    """Execute a trusted command operation without representing it as a graph stage."""
    if command.command_type is WorkflowCommandType.REBASE:
        return dict(await rebase_pr(dict(state)))
    return dict(state)
