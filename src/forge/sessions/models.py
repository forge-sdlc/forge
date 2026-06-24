"""Models for safe, read-only Forge session inspection."""

from typing import Any

from pydantic import BaseModel, Field


class SessionSummary(BaseModel):
    """Curated session state safe to expose through user-facing inspection tools."""

    ticket_key: str
    found: bool = True
    current_node: str | None = None
    status: str
    is_paused: bool = False
    is_blocked: bool = False
    retry_count: int = 0
    last_error: str | None = None
    ticket_type: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    repository: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_urls: list[str] = Field(default_factory=list)
    ci_status: str | None = None
    ci_fix_attempts: int = 0
    failed_check_names: list[str] = Field(default_factory=list)
    ai_review_status: str | None = None
    human_review_status: str | None = None
    pr_merged: bool = False
    current_task_key: str | None = None
    implemented_tasks: list[str] = Field(default_factory=list)
    repos_to_process: list[str] = Field(default_factory=list)
    repos_completed: list[str] = Field(default_factory=list)
    artifacts_present: dict[str, bool] = Field(default_factory=dict)
    recent_events: list[str] = Field(default_factory=list)
    observability_links: dict[str, str] = Field(default_factory=dict)
    raw_state_exposed: bool = False


class SessionSummaryPayload(BaseModel):
    """MCP response wrapper with a stable metadata surface."""

    summary: SessionSummary
    notes: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable response."""
        return self.model_dump(mode="json")
