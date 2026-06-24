"""Read-only observability data access through the Langfuse API."""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any

import httpx

from forge.config import get_settings

_TICKET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]*-\d+$")


class ObservabilityUnavailableError(RuntimeError):
    """Raised when Langfuse is not configured or reachable."""


def _normalize_ticket_key(ticket_key: str) -> str:
    normalized = ticket_key.strip().upper()
    if not _TICKET_KEY_PATTERN.match(normalized):
        raise ValueError("ticket_key must look like a Jira issue key, for example AISOS-123")
    return normalized


def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _window(hours: int) -> tuple[int, dt.datetime, dt.datetime]:
    bounded_hours = _bounded_int(hours, minimum=1, maximum=24 * 90)
    now = dt.datetime.now(dt.UTC)
    return bounded_hours, now - dt.timedelta(hours=bounded_hours), now


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


def _get_attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _metadata(trace: Any) -> dict[str, Any]:
    metadata = _get_attr(trace, "metadata", default={})
    return metadata if isinstance(metadata, dict) else {}


def get_langfuse_client() -> Any:
    """Create a Langfuse client using Forge settings."""
    settings = get_settings()
    public_key = settings.langfuse_public_key
    secret_key = settings.langfuse_secret_key.get_secret_value()
    if not public_key or not secret_key:
        raise ObservabilityUnavailableError("Langfuse API keys are not configured")

    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:
        raise ObservabilityUnavailableError(f"Could not initialize Langfuse client: {exc}") from exc


async def _call_langfuse(operation: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await fn(*args, **kwargs)
    except httpx.HTTPError as exc:
        raise ObservabilityUnavailableError(f"Langfuse {operation} failed: {exc}") from exc
    except Exception as exc:
        raise ObservabilityUnavailableError(f"Langfuse {operation} failed: {exc}") from exc


async def _list_traces(ticket_key: str, *, hours: int, limit: int, fields: str | None = None) -> list[Any]:
    ticket = _normalize_ticket_key(ticket_key)
    hours, from_timestamp, to_timestamp = _window(hours)
    del hours

    client = get_langfuse_client()
    response = await _call_langfuse(
        "trace.list",
        client.async_api.trace.list,
        session_id=ticket,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        limit=limit,
        order_by="timestamp.desc",
        fields=fields,
    )
    return list(_get_attr(response, "data", default=[]))


async def get_ticket_observability(
    ticket_key: str,
    *,
    hours: int = 720,
    limit: int = 50,
) -> dict[str, Any]:
    """Return safe trace/cost/timing observability for one Jira ticket."""
    ticket = _normalize_ticket_key(ticket_key)
    hours, _from_timestamp, _to_timestamp = _window(hours)
    limit = _bounded_int(limit, minimum=1, maximum=100)
    traces = await _list_traces(ticket, hours=hours, limit=limit, fields="core,metrics,io")

    totals = {
        "trace_count": len(traces),
        "total_cost": 0.0,
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "total_tokens": 0.0,
        "latency_s": 0.0,
    }
    steps: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "workflow_step": "",
            "trace_count": 0,
            "total_cost": 0.0,
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "total_tokens": 0.0,
            "latency_s": 0.0,
        }
    )

    recent_traces: list[dict[str, Any]] = []
    for trace in traces:
        metadata = _metadata(trace)
        step = str(metadata.get("workflow_step") or "unknown")
        cost = _number(_get_attr(trace, "total_cost", "totalCost"))
        input_tokens = _number(_get_attr(trace, "input_tokens", "inputTokens"))
        output_tokens = _number(_get_attr(trace, "output_tokens", "outputTokens"))
        total_tokens = _number(_get_attr(trace, "total_tokens", "totalTokens"))
        latency = _number(_get_attr(trace, "latency"))

        totals["total_cost"] += cost
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["total_tokens"] += total_tokens
        totals["latency_s"] += latency

        step_row = steps[step]
        step_row["workflow_step"] = step
        step_row["trace_count"] += 1
        step_row["total_cost"] += cost
        step_row["input_tokens"] += input_tokens
        step_row["output_tokens"] += output_tokens
        step_row["total_tokens"] += total_tokens
        step_row["latency_s"] += latency

        recent_traces.append(
            {
                "trace_id": _get_attr(trace, "id"),
                "name": _get_attr(trace, "name"),
                "timestamp": _get_attr(trace, "timestamp"),
                "workflow_step": step,
                "total_cost": cost,
                "total_tokens": total_tokens,
                "latency_s": latency,
            }
        )

    return {
        "ticket_key": ticket,
        "window_hours": hours,
        "raw_trace_data_exposed": False,
        "source": "langfuse_api",
        "totals": totals,
        "steps": sorted(steps.values(), key=lambda row: row["total_cost"], reverse=True),
        "recent_traces": _dump(recent_traces),
    }


async def get_model_usage(*, hours: int = 24, limit: int = 20) -> dict[str, Any]:
    """Return aggregate model usage from the Langfuse observations API."""
    hours, from_timestamp, to_timestamp = _window(hours)
    limit = _bounded_int(limit, minimum=1, maximum=100)
    observation_limit = max(100, limit)
    client = get_langfuse_client()
    response = await _call_langfuse(
        "legacy.observations_v1.get_many",
        client.async_api.legacy.observations_v1.get_many,
        from_start_time=from_timestamp,
        to_start_time=to_timestamp,
        limit=observation_limit,
    )

    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "model": "",
            "calls": 0,
            "total_cost": 0.0,
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "total_tokens": 0.0,
            "latency_s": 0.0,
        }
    )
    for observation in _get_attr(response, "data", default=[]):
        model = _get_attr(observation, "provided_model_name", "providedModelName", "model")
        if not model:
            continue
        usage = _get_attr(observation, "usage_details", "usageDetails", default={})
        if not isinstance(usage, dict):
            usage = {}
        row = models[str(model)]
        row["model"] = str(model)
        row["calls"] += 1
        row["total_cost"] += _number(_get_attr(observation, "total_cost", "totalCost"))
        row["input_tokens"] += _number(usage.get("input"))
        row["output_tokens"] += _number(usage.get("output"))
        row["total_tokens"] += _number(usage.get("total"))
        row["latency_s"] += _number(_get_attr(observation, "latency"))

    return {
        "window_hours": hours,
        "raw_trace_data_exposed": False,
        "source": "langfuse_api",
        "models": sorted(models.values(), key=lambda row: row["total_cost"], reverse=True)[:limit],
    }


async def get_workflow_funnel(*, hours: int = 24, limit: int = 100) -> dict[str, Any]:
    """Return workflow-step trace, cost, token, and latency aggregates."""
    hours, _from_timestamp, _to_timestamp = _window(hours)
    limit = _bounded_int(limit, minimum=1, maximum=1000)
    client = get_langfuse_client()
    response = await _call_langfuse(
        "trace.list",
        client.async_api.trace.list,
        from_timestamp=_from_timestamp,
        to_timestamp=_to_timestamp,
        limit=limit,
        order_by="timestamp.desc",
        fields="core,metrics,io",
    )

    steps: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "workflow_step": "",
            "issue_count": 0,
            "trace_count": 0,
            "total_cost": 0.0,
            "total_tokens": 0.0,
            "latency_s": 0.0,
            "_issues": set(),
        }
    )
    for trace in _get_attr(response, "data", default=[]):
        metadata = _metadata(trace)
        step = str(metadata.get("workflow_step") or "unknown")
        session_id = _get_attr(trace, "session_id", "sessionId")
        row = steps[step]
        row["workflow_step"] = step
        row["trace_count"] += 1
        row["total_cost"] += _number(_get_attr(trace, "total_cost", "totalCost"))
        row["total_tokens"] += _number(_get_attr(trace, "total_tokens", "totalTokens"))
        row["latency_s"] += _number(_get_attr(trace, "latency"))
        if session_id:
            row["_issues"].add(str(session_id))

    result = []
    for row in steps.values():
        issue_set = row.pop("_issues")
        row["issue_count"] = len(issue_set)
        result.append(row)

    return {
        "window_hours": hours,
        "raw_trace_data_exposed": False,
        "source": "langfuse_api",
        "steps": sorted(result, key=lambda row: row["trace_count"], reverse=True),
    }


async def get_observability_health(*, hours: int = 24, limit: int = 100) -> dict[str, Any]:
    """Return metadata coverage checks for traces used by Forge dashboards."""
    hours, from_timestamp, to_timestamp = _window(hours)
    limit = _bounded_int(limit, minimum=1, maximum=100)
    client = get_langfuse_client()
    response = await _call_langfuse(
        "trace.list",
        client.async_api.trace.list,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        limit=limit,
        order_by="timestamp.desc",
        fields="core,io",
    )
    traces = list(_get_attr(response, "data", default=[]))

    coverage = {
        "sampled_trace_count": len(traces),
        "missing_project_id": 0,
        "missing_ticket_type": 0,
        "missing_workflow_step": 0,
        "missing_session_id": 0,
        "latest_trace_at": None,
    }
    for trace in traces:
        metadata = _metadata(trace)
        if not metadata.get("project_id"):
            coverage["missing_project_id"] += 1
        if not metadata.get("ticket_type"):
            coverage["missing_ticket_type"] += 1
        if not metadata.get("workflow_step"):
            coverage["missing_workflow_step"] += 1
        if not _get_attr(trace, "session_id", "sessionId"):
            coverage["missing_session_id"] += 1
        if coverage["latest_trace_at"] is None:
            coverage["latest_trace_at"] = _get_attr(trace, "timestamp")

    return {
        "window_hours": hours,
        "raw_trace_data_exposed": False,
        "source": "langfuse_api",
        "metadata_coverage": _dump(coverage),
    }


async def get_session_traces(
    ticket_key: str,
    *,
    hours: int = 720,
    limit: int = 10,
    full: bool = True,
) -> dict[str, Any]:
    """Return traces for a ticket session, optionally hydrated with full trace details."""
    ticket = _normalize_ticket_key(ticket_key)
    hours, _from_timestamp, _to_timestamp = _window(hours)
    limit = _bounded_int(limit, minimum=1, maximum=50)
    traces = await _list_traces(ticket, hours=hours, limit=limit, fields=None if full else "core,metrics,io")

    client = get_langfuse_client()
    hydrated = []
    for trace in traces:
        trace_id = _get_attr(trace, "id")
        if full and trace_id:
            hydrated.append(
                await _call_langfuse("trace.get", client.async_api.trace.get, trace_id=str(trace_id))
            )
        else:
            hydrated.append(trace)

    return {
        "ticket_key": ticket,
        "window_hours": hours,
        "full_trace_data_exposed": full,
        "source": "langfuse_api",
        "traces": _dump(hydrated),
    }


async def get_trace(trace_id: str) -> dict[str, Any]:
    """Return a full Langfuse trace by trace id."""
    normalized = trace_id.strip()
    if not normalized:
        raise ValueError("trace_id must not be empty")

    client = get_langfuse_client()
    trace = await _call_langfuse("trace.get", client.async_api.trace.get, normalized)
    return {
        "trace_id": normalized,
        "full_trace_data_exposed": True,
        "source": "langfuse_api",
        "trace": _dump(trace),
    }
