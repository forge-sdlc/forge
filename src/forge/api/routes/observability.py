"""Read-only observability data endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from forge.observability.access import (
    ObservabilityUnavailableError,
    get_model_usage,
    get_observability_health,
    get_session_traces,
    get_ticket_observability,
    get_trace,
    get_workflow_funnel,
)

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])


def _backend_error(exc: ObservabilityUnavailableError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/tickets/{ticket_key}")
async def ticket_observability(
    ticket_key: str,
    hours: int = Query(default=720, ge=1, le=24 * 90),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Return safe observability aggregates for one Jira ticket."""
    try:
        return await get_ticket_observability(ticket_key, hours=hours, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)


@router.get("/tickets/{ticket_key}/traces")
async def ticket_traces(
    ticket_key: str,
    hours: int = Query(default=720, ge=1, le=24 * 90),
    limit: int = Query(default=10, ge=1, le=50),
    full: bool = Query(default=True),
) -> dict[str, Any]:
    """Return Langfuse traces for one Jira ticket session."""
    try:
        return await get_session_traces(ticket_key, hours=hours, limit=limit, full=full)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)


@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str) -> dict[str, Any]:
    """Return a full Langfuse trace by trace id."""
    try:
        return await get_trace(trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)


@router.get("/model-usage")
async def model_usage(
    hours: int = Query(default=24, ge=1, le=24 * 90),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Return aggregate model usage from Langfuse observations."""
    try:
        return await get_model_usage(hours=hours, limit=limit)
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)


@router.get("/workflow-funnel")
async def workflow_funnel(
    hours: int = Query(default=24, ge=1, le=24 * 90),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Return workflow-step issue, trace, latency, and cost aggregates."""
    try:
        return await get_workflow_funnel(hours=hours, limit=limit)
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)


@router.get("/health")
async def observability_health(
    hours: int = Query(default=24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Return metadata coverage checks for the observability layer."""
    try:
        return await get_observability_health(hours=hours)
    except ObservabilityUnavailableError as exc:
        raise _backend_error(exc)
