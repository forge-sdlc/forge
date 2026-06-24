"""Safe session summaries built from Forge checkpoint and Redis state."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from forge.config import get_settings
from forge.orchestrator.checkpointer import get_checkpoint_state, get_redis_client
from forge.sessions.models import SessionSummary, SessionSummaryPayload


class SessionNotFoundError(LookupError):
    """Raised when no persisted session state exists for a ticket."""


def _normalize_ticket_key(ticket_key: str) -> str:
    normalized = ticket_key.strip().upper()
    if not normalized:
        raise ValueError("ticket_key must not be empty")
    return normalized


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _stringify(item)
        if text:
            result.append(text)
    return result


def _failed_check_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name") or item.get("check_name") or item.get("context")
            if name:
                names.append(str(name))
        elif item:
            names.append(str(item))
    return names


def _artifact_presence(state: dict[str, Any]) -> dict[str, bool]:
    return {
        "prd": bool(state.get("prd_content")),
        "spec": bool(state.get("spec_content")),
        "rca": bool(state.get("rca_content")),
        "plan": bool(state.get("plan_content")),
        "epics": bool(state.get("epic_keys")),
        "tasks": bool(state.get("task_keys")),
        "qa_history": bool(state.get("qa_history")),
    }


def _derive_status(state: dict[str, Any]) -> str:
    if state.get("last_error"):
        return "error"
    if state.get("is_blocked"):
        return "blocked"
    if state.get("is_paused"):
        return "waiting_for_input"
    if state.get("pr_merged") or state.get("feature_completed") or state.get("bug_fix_implemented"):
        return "completed"
    return "running"


def _recent_events(logs: Iterable[Any]) -> list[str]:
    events: list[str] = []
    for entry in logs:
        if isinstance(entry, bytes):
            events.append(entry.decode(errors="replace"))
        elif entry is not None:
            events.append(str(entry))
    return events


def _observability_links(ticket_key: str) -> dict[str, str]:
    settings = get_settings()
    links: dict[str, str] = {}

    if settings.langfuse_host:
        links["langfuse"] = settings.langfuse_host.rstrip("/")

    if settings.grafana_base_url:
        base = settings.grafana_base_url.rstrip("/")
        encoded_ticket = quote(ticket_key, safe="")
        links["grafana_issue_detail"] = (
            f"{base}/d/forge-issue-detail/forge-issue-detail?"
            f"orgId=1&var-jira_issue={encoded_ticket}"
        )
        links["grafana_engineering"] = (
            f"{base}/d/forge-engineering/forge-engineering-dashboard?"
            f"orgId=1&var-jira_issue={encoded_ticket}"
        )

    return links


def build_session_summary(
    ticket_key: str,
    state: dict[str, Any] | None,
    logs: Iterable[Any] = (),
) -> SessionSummaryPayload:
    """Build a redacted session summary from persisted workflow state.

    Raw prompts, model messages, generated artifacts, tool inputs, and full trace
    metadata are intentionally not included in the result.
    """
    normalized_ticket = _normalize_ticket_key(ticket_key)
    if state is None:
        raise SessionNotFoundError(f"No Forge session found for {normalized_ticket}")

    pr_urls = _as_str_list(state.get("pr_urls"))
    current_pr_url = _stringify(state.get("current_pr_url"))
    if current_pr_url and current_pr_url not in pr_urls:
        pr_urls = [current_pr_url, *pr_urls]

    summary = SessionSummary(
        ticket_key=normalized_ticket,
        current_node=_stringify(state.get("current_node")),
        status=_derive_status(state),
        is_paused=bool(state.get("is_paused", False)),
        is_blocked=bool(state.get("is_blocked", False)),
        retry_count=int(state.get("retry_count") or 0),
        last_error=_stringify(state.get("last_error")),
        ticket_type=_stringify(state.get("ticket_type")),
        created_at=_stringify(state.get("created_at")),
        updated_at=_stringify(state.get("updated_at")),
        repository=_stringify(state.get("current_repo")),
        pr_number=state.get("current_pr_number"),
        pr_url=current_pr_url,
        pr_urls=pr_urls,
        ci_status=_stringify(state.get("ci_status")),
        ci_fix_attempts=int(state.get("ci_fix_attempts") or 0),
        failed_check_names=_failed_check_names(state.get("ci_failed_checks")),
        ai_review_status=_stringify(state.get("ai_review_status")),
        human_review_status=_stringify(state.get("human_review_status")),
        pr_merged=bool(state.get("pr_merged", False)),
        current_task_key=_stringify(state.get("current_task_key")),
        implemented_tasks=_as_str_list(state.get("implemented_tasks")),
        repos_to_process=_as_str_list(state.get("repos_to_process")),
        repos_completed=_as_str_list(state.get("repos_completed")),
        artifacts_present=_artifact_presence(state),
        recent_events=_recent_events(logs),
        observability_links=_observability_links(normalized_ticket),
    )
    return SessionSummaryPayload(
        summary=summary,
        notes=[
            "This summary is read-only and excludes raw prompts, model messages, generated artifacts, and tool inputs."
        ],
    )


async def get_session_summary(ticket_key: str, logs_limit: int = 0) -> SessionSummaryPayload:
    """Fetch and summarize a Forge session by Jira ticket key."""
    normalized_ticket = _normalize_ticket_key(ticket_key)
    state = await get_checkpoint_state(normalized_ticket)
    if state is None:
        raise SessionNotFoundError(f"No Forge session found for {normalized_ticket}")

    logs: list[Any] = []
    if logs_limit > 0:
        redis_client = await get_redis_client()
        logs = await redis_client.lrange(f"forge:logs:{normalized_ticket}", 0, logs_limit - 1)
    return build_session_summary(normalized_ticket, state, logs)
