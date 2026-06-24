"""Base workflow classes and state definitions."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages

from forge.models.workflow import TicketType


class BaseState(TypedDict, total=False):
    """State shared by ALL workflows."""

    # Identity
    thread_id: str
    ticket_key: str

    # Event origin
    event_type: str

    # Execution control
    current_node: str
    is_paused: bool
    is_blocked: bool
    retry_count: int
    last_error: str | None

    # Timestamps
    created_at: str
    updated_at: str

    # Feedback (human-in-the-loop)
    feedback_comment: str | None
    revision_requested: bool
    yolo_mode: bool  # When True, approval gates auto-pass without human input

    # Message history
    messages: Annotated[list[Any], add_messages]
    context: dict[str, Any]


class PRIntegrationState(TypedDict, total=False):
    """Mixin for workflows that create PRs."""

    workspace_path: str | None
    pr_urls: list[str]
    current_pr_url: str | None
    current_pr_number: int | None
    current_repo: str | None
    repos_to_process: list[str]
    repos_completed: list[str]
    implemented_tasks: list[str]
    current_task_key: str | None
    fork_owner: str | None
    fork_repo: str | None
    merge_conflicts: list[str]
    local_review_attempts: int
    local_review_pass_number: int


class CIIntegrationState(TypedDict, total=False):
    """Mixin for workflows that use CI."""

    ci_status: str | None
    ci_failed_checks: list[dict[str, Any]]
    ci_fix_attempts: int
    ci_skipped_checks: list[str]
    current_attempt: int
    max_attempts: int


class ReviewIntegrationState(TypedDict, total=False):
    """Mixin for workflows with review stages."""

    ai_review_status: str | None
    ai_review_results: list[dict[str, Any]]
    human_review_status: str | None
    pr_merged: bool
    review_comments: list[dict[str, Any]]
    contested_comments: list[dict[str, Any]]
    review_response_posted: bool


class BaseWorkflow(ABC):
    """Base class all workflows must extend."""

    name: str
    description: str

    @property
    @abstractmethod
    def state_schema(self) -> type:
        """Return the TypedDict state class for this workflow."""
        ...

    @abstractmethod
    def matches(self, ticket_type: TicketType, labels: list[str], event: dict[str, Any]) -> bool:
        """Return True if this workflow should handle the given ticket/event."""
        ...

    @abstractmethod
    def build_graph(self) -> StateGraph[Any]:
        """Construct and return the LangGraph StateGraph."""
        ...

    def create_initial_state(self, ticket_key: str, **kwargs: Any) -> dict[str, Any]:
        """Create initial state for a new workflow run."""
        now = datetime.utcnow().isoformat()
        return {
            "thread_id": ticket_key,
            "ticket_key": ticket_key,
            "current_node": "start",
            "is_paused": False,
            "is_blocked": False,
            "retry_count": 0,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "context": {},
            **kwargs,
        }
