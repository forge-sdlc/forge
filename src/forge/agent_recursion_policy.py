"""Resolve the host-agent graph limit from global and Jira project settings."""

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.integrations.jira.client import JiraClient


AGENT_RECURSION_PROPERTY = "forge.agent_recursion_limit"
MAX_READ_ATTEMPTS = 3
READ_ATTEMPT_TIMEOUT = 10.0
READ_TOTAL_BUDGET = 30.0
READ_BACKOFF = (0.5, 1.0)
logger = logging.getLogger(__name__)


def validate_project_agent_recursion_limit(raw: Any, project_key: str) -> int | None:
    """Validate one optional JSON project override without applying fallback."""
    if raw is None:
        return None
    if type(raw) is not int or raw < 1:
        raise ValueError(f"{AGENT_RECURSION_PROPERTY} for {project_key} must be a positive integer")
    return raw


def _monotonic() -> float:
    return time.monotonic()


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


async def _await_with_timeout(awaitable: Any, timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=timeout)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_date = parsedate_to_datetime(value)
            if retry_date.tzinfo is None:
                retry_date = retry_date.replace(tzinfo=UTC)
            delay = max(0.0, (retry_date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


async def read_agent_recursion_property(jira: "JiraClient", project_key: str) -> Any | None:
    """Read one project property with bounded transport retries only."""
    deadline = _monotonic() + READ_TOTAL_BUDGET
    for attempt in range(1, MAX_READ_ATTEMPTS + 1):
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Reading {AGENT_RECURSION_PROPERTY} for {project_key} exceeded the time budget"
            )
        retry_after = None
        try:
            return await _await_with_timeout(
                jira.get_project_property(project_key, AGENT_RECURSION_PROPERTY),
                min(READ_ATTEMPT_TIMEOUT, remaining),
            )
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404:
                return None
            if status != 429 and status < 500:
                raise RuntimeError(
                    f"Reading {AGENT_RECURSION_PROPERTY} for {project_key} failed: HTTP {status}"
                ) from None
            reason = f"HTTP {status}"
            retry_after = _retry_after_seconds(error.response.headers.get("Retry-After"))
        except (httpx.TransportError, TimeoutError) as error:
            reason = "timeout" if isinstance(error, TimeoutError) else "transport error"
        except (ValueError, KeyError, TypeError):
            raise ValueError(
                f"Malformed {AGENT_RECURSION_PROPERTY} response for {project_key}"
            ) from None

        if attempt == MAX_READ_ATTEMPTS:
            raise RuntimeError(
                f"Reading {AGENT_RECURSION_PROPERTY} for {project_key} failed "
                f"after {attempt} attempts: {reason}"
            ) from None
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Reading {AGENT_RECURSION_PROPERTY} for {project_key} exceeded the time budget"
            ) from None
        delay = retry_after if retry_after is not None else READ_BACKOFF[attempt - 1]
        if delay >= remaining:
            raise RuntimeError(
                f"Reading {AGENT_RECURSION_PROPERTY} for {project_key} exceeded the retry budget"
            ) from None
        await _sleep(delay)
    raise AssertionError("unreachable")


async def resolve_agent_recursion_limit_for_project(
    settings: "Settings",
    project_key: str | None,
    *,
    jira: "JiraClient | None" = None,
) -> int:
    """Resolve one host invocation's effective graph limit."""
    owned_jira = None
    if not project_key:
        limit = settings.agent_recursion_limit
        source = "global"
    else:
        if jira is None:
            from forge.integrations.jira.client import JiraClient

            owned_jira = JiraClient(settings=settings)
            jira = owned_jira
        try:
            raw = await read_agent_recursion_property(jira, project_key)
            override = validate_project_agent_recursion_limit(raw, project_key)
        finally:
            if owned_jira is not None:
                await owned_jira.close()
        limit = settings.agent_recursion_limit if override is None else override
        source = "global" if override is None else "project"
    logger.info("Host agent recursion limit=%s source=%s project=%s", limit, source, project_key)
    return limit
