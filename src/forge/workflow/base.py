"""Base workflow classes and state definitions."""

import operator
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages

from forge.models.workflow import TicketType
from forge.workflow.pr_state import PullRequestState


class ArtifactRef(TypedDict, total=False):
    """Checkpoint-safe reference to an artifact used as implementation context.

    ``kind`` identifies the planning level (for example ``task``, ``plan``,
    ``spec``, or ``prd``). ``source`` identifies where it came from, normally a
    Jira issue key or a workflow-state field. Content is optional so callers can
    persist either an inline snapshot or only identity and digest metadata.
    """

    id: str
    kind: str
    source: str
    content: str
    repository: str | None
    approved: bool
    digest: str
    jira_key: str | None
    summary: str | None
    provenance: dict[str, Any]
    revision: int
    status: str
    approved_digest: str | None
    input_artifact_ids: list[str]
    parent_artifact_id: str | None
    child_artifact_ids: list[str]
    derived_work_unit_ids: list[str]
    created_by_node: str


class WorkUnit(TypedDict, total=False):
    """Normalized, repository-scoped unit of implementation work.

    Jira Tasks and taskless artifact-based work use the same representation.
    Workflow-specific legacy task fields remain available during migration.
    """

    id: str
    kind: str
    key: str | None
    jira_key: str | None
    repository: str
    status: str
    instructions: str
    source_digest: str
    source_artifact_ids: list[str]
    context_artifact_ids: list[str]
    dependency_work_unit_ids: list[str]
    provenance: dict[str, Any]


class RepositoryRef(TypedDict, total=False):
    """Repository scope and traversal status for a workflow."""

    name: str
    source: str
    status: str
    work_unit_ids: list[str]
    provenance: dict[str, Any]


class ValidationResult(TypedDict, total=False):
    """Durable validation evidence for one repository/work unit."""

    id: str
    repository: str
    work_unit_id: str | None
    kind: str
    status: str
    summary: str
    evidence: dict[str, Any]


class PublicationRef(TypedDict, total=False):
    """Durable commit/push/pull-request outcome for a repository."""

    repository: str
    commit_sha: str | None
    branch: str | None
    pr_url: str | None
    status: str


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

    # Declarative workflow identity. Built-in workflows leave these unset.
    workflow_name: str
    workflow_revision: int
    workflow_digest: str
    workflow_state_profile: str
    workflow_project_key: str
    workflow_transition_count: int

    # Generic node-contract capabilities and durable precondition audit trail.
    # Missing capability keys preserve legacy inference; explicit booleans are
    # authoritative for newer workflows.
    capabilities: dict[str, bool]
    precondition_result: dict[str, Any]
    precondition_history: list[dict[str, Any]]

    # Normalized implementation inputs. These fields are optional so checkpoints
    # written before work resolution was introduced remain valid.
    artifacts: list[ArtifactRef]
    work_units: list[WorkUnit]
    current_work_unit_id: str | None
    work_resolution: dict[str, Any]
    repositories: list[RepositoryRef]
    current_repository: str | None
    validations: list[ValidationResult]
    publications: list[PublicationRef]
    node_outcome: str | None


class HandoffState(TypedDict):
    """Durable task-continuity summary for one repository."""

    content: str
    task_key: str
    captured_at: str


class PRIntegrationState(TypedDict, total=False):
    """Mixin for workflows that create PRs."""

    workspace_path: str | None
    pr_urls: list[str]
    pull_requests: dict[str, PullRequestState]
    current_pr_url: str | None
    current_pr_number: int | None
    pr_created_comment_posted: bool
    current_repo: str | None
    repos_to_process: list[str]
    repos_completed: list[str]
    implemented_tasks: list[str]
    jira_completed_tasks: list[str]
    current_task_key: str | None
    fork_owner: str | None
    fork_repo: str | None
    merge_conflicts: list[str]
    local_review_attempts: int
    local_review_pass_number: int
    implementation_push_pending: bool
    implementation_push_pending_task: str | None
    persistence_retry_count: int
    review_push_pending: bool
    review_push_pending_updates: dict[str, Any]
    review_exhaustion_report: Annotated[dict[str, Any], operator.or_]
    handoffs: dict[str, HandoffState]


class CIIntegrationState(TypedDict, total=False):
    """Mixin for workflows that use CI."""

    ci_status: str | None
    ci_failed_checks: list[dict[str, Any]]
    ci_skipped_checks: list[str]
    ci_fix_attempt: int
    ci_fix_max_attempts: int
    pending_ci_event: bool


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
