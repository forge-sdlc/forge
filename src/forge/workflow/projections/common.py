"""Shared projection helpers for contract-backed stations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from forge.domain import StationInvocationIdentity, WorkflowIdentity, stable_identity


def project_workflow_identity(state: Mapping[str, Any]) -> WorkflowIdentity:
    ticket_key = str(state.get("ticket_key") or "local")
    return WorkflowIdentity(
        run_id=str(state.get("thread_id") or ticket_key),
        workflow_name=str(state.get("workflow_name") or state.get("ticket_type") or "legacy"),
        definition_revision=int(state.get("workflow_revision") or 1),
        definition_digest=state.get("workflow_digest"),
    )


def project_invocation_identity(
    state: Mapping[str, Any], station_name: str, discriminator: str = "default"
) -> StationInvocationIdentity:
    workflow = project_workflow_identity(state)
    return StationInvocationIdentity(
        invocation_id=stable_identity(
            "station-invocation",
            {
                "run_id": workflow.run_id,
                "station": station_name,
                "discriminator": discriminator,
                "attempt": int(state.get("retry_count") or 0) + 1,
            },
        ),
        station_name=station_name,
    )


def project_requested_at(state: Mapping[str, Any]) -> datetime:
    value = state.get("updated_at")
    return datetime.fromisoformat(str(value)) if value else datetime(1970, 1, 1, tzinfo=UTC)
