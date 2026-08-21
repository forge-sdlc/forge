"""Health check endpoints for monitoring and load balancers."""

import logging
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel

from forge import __version__
from forge.orchestrator.checkpointer import get_redis_client
from forge.queue.producer import JIRA_STREAM, LEGACY_SOURCE_CONTROL_STREAM, SOURCE_CONTROL_STREAM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["health"])


class HealthResponse(BaseModel):
    """Health check response model."""

    status: str
    version: str
    redis: str
    queue_depth: int = 0


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "Service is healthy"},
        503: {"description": "Service is unhealthy"},
    },
)
async def health_check() -> Any:
    """Check the health of the service and its dependencies.

    Returns:
        HealthResponse with status of all components.
    """
    redis_status = "disconnected"
    queue_depth = 0

    try:
        redis_client = await get_redis_client()
        await redis_client.ping()
        redis_status = "connected"

        # Get approximate queue depth
        try:
            jira_len = await redis_client.xlen(JIRA_STREAM)
            source_control_len = await redis_client.xlen(SOURCE_CONTROL_STREAM)
            legacy_len = await redis_client.xlen(LEGACY_SOURCE_CONTROL_STREAM)
            queue_depth = jira_len + source_control_len + legacy_len
        except Exception:
            pass  # Streams may not exist yet

    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return HealthResponse(
            status="unhealthy",
            version=__version__,
            redis=redis_status,
            queue_depth=queue_depth,
        )

    return HealthResponse(
        status="healthy",
        version=__version__,
        redis=redis_status,
        queue_depth=queue_depth,
    )


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness_check() -> dict[str, str]:
    """Kubernetes readiness probe endpoint.

    Returns:
        Simple ready status.
    """
    return {"status": "ready"}


@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness_check() -> dict[str, str]:
    """Kubernetes liveness probe endpoint.

    Returns:
        Simple alive status.
    """
    return {"status": "alive"}
