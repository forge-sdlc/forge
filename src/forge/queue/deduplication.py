"""Shared Redis keys and retention policy for event deduplication."""

DEDUP_TTL_SECONDS = 86400
DEDUP_KEY_PREFIX = "forge:dedup:"
