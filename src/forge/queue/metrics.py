"""Bounded-cardinality metrics for durable queue dispatch."""

from prometheus_client import Counter, Gauge

QUEUE_ACTIVE_DELIVERIES = Gauge(
    "forge_queue_active_deliveries",
    "Queue delivery tasks currently dispatched in this process.",
)
QUEUE_BUFFERED_DELIVERIES = Gauge(
    "forge_queue_buffered_deliveries",
    "Reserved delivery capacity not yet dispatched to a task.",
)
QUEUE_RECLAIMED_DELIVERIES = Counter(
    "forge_queue_reclaimed_deliveries_total",
    "Pending Redis stream deliveries reclaimed for recovery.",
)
QUEUE_LEASE_LOSSES = Counter(
    "forge_queue_lease_losses_total",
    "Queue ownership leases lost before durable completion.",
    ["kind"],
)
