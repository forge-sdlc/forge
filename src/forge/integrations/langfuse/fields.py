"""Configurable Langfuse trace tag and metadata fields.

Admins configure which fields to include as tags/metadata via env vars:
  LANGFUSE_TRACE_TAGS=ticket_type,project_id,workflow_step
  LANGFUSE_TRACE_METADATA=ticket_key,ticket_type,project_id,retry_count
"""

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class TracingField(StrEnum):
    """Available fields for Langfuse trace tags and metadata."""

    TICKET_KEY = "ticket_key"
    TICKET_TYPE = "ticket_type"
    PROJECT_ID = "project_id"
    WORKFLOW_STEP = "workflow_step"
    REPO = "repo"
    PR_NUMBER = "pr_number"
    CI_STATUS = "ci_status"
    EVENT_SOURCE = "event_source"
    EVENT_TYPE = "event_type"
    RETRY_COUNT = "retry_count"
    SYSTEM_PROMPT_LENGTH = "system_prompt_length"
    LLM_MODEL = "llm_model"

    @property
    def tag_eligible(self) -> bool:
        return self not in _METADATA_ONLY_FIELDS


_METADATA_ONLY_FIELDS = frozenset({TracingField.RETRY_COUNT, TracingField.SYSTEM_PROMPT_LENGTH})


def resolve_field(field: TracingField, state: dict[str, Any]) -> str | None:
    """Resolve a single tracing field from workflow state.

    Args:
        field: The field to resolve.
        state: Workflow state dict.

    Returns:
        String value or None if the data isn't available.
    """
    resolver = _RESOLVERS.get(field)
    if resolver is None:
        return None
    return resolver(state)


def _resolve_ticket_key(state: dict[str, Any]) -> str | None:
    val = state.get("ticket_key")
    return str(val) if val is not None else None


def _resolve_ticket_type(state: dict[str, Any]) -> str | None:
    val = state.get("ticket_type")
    return str(val) if val is not None else None


def _resolve_project_id(state: dict[str, Any]) -> str | None:
    ticket_key = state.get("ticket_key")
    if not ticket_key or "-" not in str(ticket_key):
        return None
    return str(ticket_key).rsplit("-", 1)[0]


def _resolve_workflow_step(state: dict[str, Any]) -> str | None:
    val = state.get("current_node")
    return str(val) if val is not None else None


def _resolve_repo(state: dict[str, Any]) -> str | None:
    val = state.get("repo") or state.get("current_repo")
    return str(val) if val is not None else None


def _resolve_pr_number(state: dict[str, Any]) -> str | None:
    val = state.get("pr_number") or state.get("current_pr_number")
    return str(val) if val is not None else None


def _resolve_ci_status(state: dict[str, Any]) -> str | None:
    val = state.get("ci_status")
    return str(val) if val is not None else None


def _resolve_event_source(state: dict[str, Any]) -> str | None:
    val = state.get("event_source")
    if val is not None:
        return str(val)
    ctx = state.get("context")
    if isinstance(ctx, dict):
        val = ctx.get("source")
        if val is not None:
            return str(val)
    return None


def _resolve_event_type(state: dict[str, Any]) -> str | None:
    val = state.get("event_type")
    return str(val) if val is not None else None


def _resolve_retry_count(state: dict[str, Any]) -> str | None:
    val = state.get("retry_count")
    return str(val) if val is not None else None


def _resolve_system_prompt_length(state: dict[str, Any]) -> str | None:
    val = state.get("system_prompt_length")
    return str(val) if val is not None else None


def _resolve_llm_model(state: dict[str, Any]) -> str | None:
    val = state.get("llm_model")
    return str(val) if val is not None else None


_RESOLVERS: dict[TracingField, Any] = {
    TracingField.TICKET_KEY: _resolve_ticket_key,
    TracingField.TICKET_TYPE: _resolve_ticket_type,
    TracingField.PROJECT_ID: _resolve_project_id,
    TracingField.WORKFLOW_STEP: _resolve_workflow_step,
    TracingField.REPO: _resolve_repo,
    TracingField.PR_NUMBER: _resolve_pr_number,
    TracingField.CI_STATUS: _resolve_ci_status,
    TracingField.EVENT_SOURCE: _resolve_event_source,
    TracingField.EVENT_TYPE: _resolve_event_type,
    TracingField.RETRY_COUNT: _resolve_retry_count,
    TracingField.SYSTEM_PROMPT_LENGTH: _resolve_system_prompt_length,
    TracingField.LLM_MODEL: _resolve_llm_model,
}


def parse_trace_fields(config_str: str, *, allow_tags: bool) -> list[TracingField]:
    """Parse a comma-separated config string into validated TracingField list.

    Args:
        config_str: Comma-separated field names (e.g., "ticket_key,ticket_type").
        allow_tags: If True, validates tag eligibility; if False, allows all fields.

    Returns:
        List of valid TracingField values. Invalid names are warned and skipped.
    """
    if not config_str or not config_str.strip():
        return []

    available = ", ".join(sorted(f.value for f in TracingField))
    result: list[TracingField] = []

    for raw in config_str.split(","):
        name = raw.strip()
        if not name:
            continue

        try:
            field = TracingField(name)
        except ValueError:
            logger.warning(
                "Invalid Langfuse trace field '%s' - not a recognized field name. Available: %s",
                name,
                available,
            )
            continue

        if allow_tags and not field.tag_eligible:
            logger.warning(
                "Field '%s' is not eligible for tags (quantitative field) - skipping",
                name,
            )
            continue

        result.append(field)

    return result


def resolve_trace_fields(state: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Resolve configured tracing fields from workflow state.

    Reads the configured tag/metadata fields from settings, resolves each
    against the state dict, and returns the results. Fields that resolve
    to None are silently omitted.

    Args:
        state: Workflow state dict containing trace data.

    Returns:
        (tags, metadata) — tags is a list of raw string values,
        metadata is a dict of field_name -> string value.
    """
    from forge.config import get_settings

    settings = get_settings()

    tags: list[str] = []
    for field in settings.trace_tag_fields:
        value = resolve_field(field, state)
        if value:
            tags.append(value)

    metadata: dict[str, Any] = {}
    for field in settings.trace_metadata_fields:
        value = resolve_field(field, state)
        if value is not None:
            metadata[field.value] = value

    return tags, metadata
