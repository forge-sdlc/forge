"""Bug workflow state definition."""

from datetime import datetime
from typing import Any

from forge.config import get_settings
from forge.models.workflow import TicketType
from forge.workflow.base import (
    BaseState,
    CIIntegrationState,
    PRIntegrationState,
    ReviewIntegrationState,
)


class BugState(
    BaseState, PRIntegrationState, CIIntegrationState, ReviewIntegrationState, total=False
):
    """State specific to Bug workflow."""

    ticket_type: TicketType

    # Bug-specific
    rca_content: str | None
    bug_fix_implemented: bool
    tdd_approach: bool

    # Q&A mode
    qa_history: list[dict[str, str]]  # List of {question, answer, artifact_type, timestamp}
    generation_context: dict[str, Any]  # Stored context from generation
    is_question: bool  # Current comment is a question (not feedback)

    # Triage stage
    triage_passed: bool
    triage_missing_fields: list[str]

    # Analysis / reflection loop
    reflection_count: int
    reflection_critique: str | None
    rca_options: list[dict]  # [{title, description, tradeoffs}, ...]
    reproducibility_assessment: str | None

    # Option selection
    selected_fix_option: int | None
    selected_fix_approach: dict | None
    rca_comment_posted: bool  # Guard against re-posting the RCA comment on gate re-entry

    # Planning
    plan_content: str | None
    linked_task_keys: list[str]
    task_keys: list[str]
    tasks_by_repo: dict[str, list[str]]

    # Qualitative review (implementation phase)
    local_review_verdict: str | None  # "adequate" | "tests_incomplete" | "symptom_only"
    qualitative_feedback: str | None
    qualitative_retry_count: int
    qualitative_review_failed: bool

    # reflect_rca container failure counter (separate from analyze_bug's retry_count)
    reflect_rca_retry_count: int


def create_initial_bug_state(ticket_key: str, **kwargs: Any) -> BugState:
    """Create initial state for a new Bug workflow run."""
    now = datetime.utcnow().isoformat()
    settings = get_settings()

    # Default values - can be overridden by kwargs
    defaults = {
        "thread_id": ticket_key,
        "ticket_key": ticket_key,
        "ticket_type": TicketType.BUG,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "rca_content": None,
        "bug_fix_implemented": False,
        "workspace_path": None,
        "pr_urls": [],
        "fork_owner": None,
        "fork_repo": None,
        "merge_conflicts": [],
        "local_review_attempts": 0,
        "local_review_pass_number": 1,
        "tdd_approach": False,
        "ci_status": None,
        "current_pr_url": None,
        "current_pr_number": None,
        "current_repo": None,
        "repos_to_process": [],
        "repos_completed": [],
        "implemented_tasks": [],
        "current_task_key": None,
        "ci_failed_checks": [],
        "ci_skipped_checks": [],
        "ci_fix_attempt": 0,
        "ci_fix_max_attempts": settings.ci_fix_max_retries,
        "ai_review_status": None,
        "ai_review_results": [],
        "human_review_status": None,
        "pr_merged": False,
        "feedback_comment": None,
        "revision_requested": False,
        "messages": [],
        "context": {},
        "qa_history": [],
        "generation_context": {},
        "is_question": False,
        # Triage stage
        "triage_passed": False,
        "triage_missing_fields": [],
        # Analysis / reflection loop
        "reflection_count": 0,
        "reflection_critique": None,
        "rca_options": [],
        "reproducibility_assessment": None,
        # Option selection
        "selected_fix_option": None,
        "selected_fix_approach": None,
        "rca_comment_posted": False,
        # Planning
        "plan_content": None,
        "linked_task_keys": [],
        "task_keys": [],
        "tasks_by_repo": {},
        # Qualitative review
        "local_review_verdict": None,
        "qualitative_feedback": None,
        "qualitative_retry_count": 0,
        "qualitative_review_failed": False,
        "reflect_rca_retry_count": 0,
        "yolo_mode": False,
    }

    # Merge with kwargs, letting kwargs override defaults
    defaults.update(kwargs)

    return BugState(**defaults)
