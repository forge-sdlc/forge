"""Feature workflow state definition."""

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


class FeatureState(
    BaseState, PRIntegrationState, CIIntegrationState, ReviewIntegrationState, total=False
):
    """State specific to Feature workflow."""

    ticket_type: TicketType

    # Artifacts
    prd_content: str
    spec_content: str

    # Epic/Task tracking
    epic_keys: list[str]
    current_epic_key: str | None
    task_keys: list[str]
    tasks_by_repo: dict[str, list[str]]

    # Completion tracking
    tasks_completed: bool
    epics_completed: bool
    feature_completed: bool

    # Parallel execution
    parallel_execution_enabled: bool
    parallel_branch_id: int | None
    parallel_total_branches: int | None

    # Q&A mode
    # List of {question, answer, artifact_type, timestamp}
    qa_history: list[dict[str, str]]
    generation_context: dict[str, Any]  # Stored context from generation
    is_question: bool  # Current comment is a question (not feedback)

    # PRD PR tracking (enhancement proposal flow)
    prd_pr_url: str | None
    prd_pr_number: int | None
    prd_pr_repo: str | None
    prd_pr_branch: str | None
    prd_pr_file_path: str | None

    # Spec PR tracking (enhancement proposal flow)
    spec_pr_url: str | None
    spec_pr_number: int | None
    spec_pr_repo: str | None
    spec_pr_branch: str | None
    spec_pr_file_path: str | None


def create_initial_feature_state(ticket_key: str, **kwargs: Any) -> FeatureState:
    """Create initial state for a new Feature workflow run."""
    now = datetime.utcnow().isoformat()
    settings = get_settings()

    # Default values - can be overridden by kwargs
    defaults = {
        "thread_id": ticket_key,
        "ticket_key": ticket_key,
        "ticket_type": TicketType.FEATURE,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
        "prd_content": "",
        "spec_content": "",
        "epic_keys": [],
        "current_epic_key": None,
        "task_keys": [],
        "tasks_by_repo": {},
        "workspace_path": None,
        "pr_urls": [],
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
        "parallel_execution_enabled": True,
        "parallel_branch_id": None,
        "parallel_total_branches": None,
        "ci_failed_checks": [],
        "ci_skipped_checks": [],
        "ci_fix_attempt": 0,
        "ci_fix_max_attempts": settings.ci_fix_max_retries,
        "ai_review_status": None,
        "ai_review_results": [],
        "human_review_status": None,
        "pr_merged": False,
        "review_comments": [],
        "contested_comments": [],
        "review_response_posted": False,
        "tasks_completed": False,
        "epics_completed": False,
        "feature_completed": False,
        "feedback_comment": None,
        "revision_requested": False,
        "messages": [],
        "context": {},
        "qa_history": [],
        "generation_context": {},
        "is_question": False,
        "prd_pr_url": None,
        "prd_pr_number": None,
        "prd_pr_repo": None,
        "prd_pr_branch": None,
        "prd_pr_file_path": None,
        "spec_pr_url": None,
        "spec_pr_number": None,
        "spec_pr_repo": None,
        "spec_pr_branch": None,
        "spec_pr_file_path": None,
        "yolo_mode": False,
    }

    # Merge with kwargs, letting kwargs override defaults
    defaults.update(kwargs)

    return FeatureState(**defaults)
