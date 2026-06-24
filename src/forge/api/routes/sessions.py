"""Read-only session inspection endpoints."""

from fastapi import APIRouter, HTTPException, Query, status

from forge.sessions.models import SessionSummaryPayload
from forge.sessions.summary import SessionNotFoundError, get_session_summary

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.get(
    "/{ticket_key}/summary",
    response_model=SessionSummaryPayload,
    responses={
        200: {"description": "Safe session summary"},
        404: {"description": "Session not found"},
    },
)
async def session_summary(
    ticket_key: str,
    logs_limit: int = Query(
        default=0,
        ge=0,
        le=50,
        description=(
            "Optional number of Redis log entries to include. Defaults to 0 so "
            "the public endpoint exposes checkpoint-derived summary only."
        ),
    ),
) -> SessionSummaryPayload:
    """Return a safe read-only summary for a Forge session.

    This endpoint lets users inspect their session through Forge API without
    direct Redis, Langfuse, or Grafana credentials.
    """
    try:
        return await get_session_summary(ticket_key, logs_limit=logs_limit)
    except SessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Forge session found for {ticket_key.strip().upper()}",
        )
