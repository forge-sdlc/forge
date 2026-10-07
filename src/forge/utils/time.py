"""Time helpers for existing timezone-free UTC persistence fields."""

from datetime import UTC, datetime


def utc_now_naive() -> datetime:
    """Return UTC without tzinfo for legacy timestamp wire formats."""
    return datetime.now(UTC).replace(tzinfo=None)
