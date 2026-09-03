"""Read-only Org Pulse integration endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from forge.api.routes.executions import load_execution_read_model, require_operator
from forge.integrations.org_pulse import OrgPulseExecution

router = APIRouter(prefix="/api/v1/org-pulse", tags=["org-pulse"])


@router.get(
    "/workflows/{ticket_key}",
    response_model=OrgPulseExecution,
    summary="Get the dashboard-safe execution summary",
)
async def get_pulse_execution(
    ticket_key: str,
    _authorized: Annotated[None, Depends(require_operator)],
) -> OrgPulseExecution:
    """Return the stable summary used by Org Pulse.

    The endpoint is intentionally authenticated and read-only. Org Pulse should
    retain the ``schema_version`` field and tolerate additive fields in future
    responses.
    """
    model = await load_execution_read_model(ticket_key)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Workflow {ticket_key} was not found")
    return OrgPulseExecution.from_execution(model)
