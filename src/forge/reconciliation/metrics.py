"""Bounded-cardinality metrics for durable observation processing."""

from __future__ import annotations

import logging
import time
from typing import Any

from prometheus_client import Gauge

logger = logging.getLogger(__name__)

PENDING_PROCESSING_COUNT = Gauge(
    "forge_observation_processing_pending",
    "Number of accepted observations awaiting workflow completion.",
)
OLDEST_PENDING_PROCESSING_AGE = Gauge(
    "forge_observation_processing_oldest_age_seconds",
    "Age in seconds of the oldest accepted observation awaiting workflow completion.",
)
_oldest_pending_timestamp: float | None = None


def _oldest_pending_age() -> float:
    if _oldest_pending_timestamp is None:
        return 0.0
    return max(0.0, time.time() - _oldest_pending_timestamp)


OLDEST_PENDING_PROCESSING_AGE.set_function(_oldest_pending_age)


async def refresh_processing_metrics(redis: Any) -> None:
    """Refresh process-local snapshots without affecting durable ledger writes."""
    global _oldest_pending_timestamp
    try:
        pending_key = "forge:observations:processing:pending"
        count = await redis.zcard(pending_key)
        oldest = await redis.zrange(pending_key, 0, 0, withscores=True)
        PENDING_PROCESSING_COUNT.set(count)
        _oldest_pending_timestamp = float(oldest[0][1]) if oldest else None
    except Exception:
        # Metrics are an operational aid and must never turn a committed
        # acceptance/finalization into a transport retry.
        logger.debug("Unable to refresh observation processing metrics", exc_info=True)
