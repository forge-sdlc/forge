"""Low-cardinality diagnostics for rejected stale workflow writes."""

from prometheus_client import Counter

STALE_WRITES = Counter(
    "forge_ticket_stale_writes_rejected_total",
    "Durable workflow operations rejected because ticket ownership was lost",
)
