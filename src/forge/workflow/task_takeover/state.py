"""Task Takeover workflow state definition."""

from datetime import datetime
from typing import Any, TypedDict, cast

from forge.models.workflow import TicketType
from forge.workflow.base import (
    BaseState,
    CIIntegrationState,
    PRIntegrationState,
    ReviewIntegrationState,
)


class CommitInfo(TypedDict, total=False):
    """Information about the implementation commit."""

    sha: str | None
    message: str | None
    committed: bool


class TaskTakeoverState(
    BaseState, PRIntegrationState, CIIntegrationState, ReviewIntegrationState, total=False
):
    """State specific to Task Takeover workflow."""

    ticket_type: TicketType
    plan_content: str | None
    triage_passed: bool
    triage_missing_fields: list[str]
    review_verdict: str | None
    review_feedback: str | None
    qualitative_review_retry_count: int
    qualitative_review_failed: bool
    commit_info: CommitInfo


def create_initial_task_takeover_state(ticket_key: str, **kwargs: Any) -> TaskTakeoverState:
    """Create initial state for a new Task Takeover workflow run."""
    now = datetime.utcnow().isoformat()
    defaults: dict[str, Any] = {
        "thread_id": ticket_key,
        "ticket_key": ticket_key,
        "ticket_type": TicketType.TASK,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "workspace_path": None,
        "pr_urls": [],
        "pull_requests": {},
        "fork_owner": None,
        "fork_repo": None,
        "merge_conflicts": [],
        "local_review_attempts": 0,
        "local_review_pass_number": 1,
        "ci_status": None,
        "current_pr_url": None,
        "current_pr_number": None,
        "current_repo": None,
        "repos_to_process": [],
        "repos_completed": [],
        "implemented_tasks": [],
        "current_task_key": None,
        "triage_passed": False,
        "triage_missing_fields": [],
        "plan_content": None,
        "review_verdict": None,
        "review_feedback": None,
        "qualitative_review_retry_count": 0,
        "qualitative_review_failed": False,
        "commit_info": {
            "sha": None,
            "message": None,
            "committed": False,
        },
        "implementation_push_pending": False,
        "implementation_push_pending_task": None,
        "persistence_retry_count": 0,
        "review_exhaustion_report": {},
        "handoffs": {},
    }
    defaults.update(kwargs)
    return cast(TaskTakeoverState, defaults)
