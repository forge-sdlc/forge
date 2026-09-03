"""Task approval gate for human-in-the-loop review before implementation.

The task approval workflow uses labels:
- forge:task-pending  - Tasks awaiting approval before implementation
- forge:task-approved - Tasks approved (triggers implementation)

To approve: Change label from forge:task-pending to forge:task-approved
To request revision: Add a comment starting with ! (keeps forge:task-pending)
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from langgraph.graph import END

from forge.api.routes.metrics import record_approval, record_revision_requested
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.projections.approval import project_approval
from forge.workflow.reducers.approval import reduce_approval_gate
from forge.workflow.stations.approval import ApprovalDisposition, run_approval_station
from forge.workflow.utils import update_state_timestamp

if TYPE_CHECKING:
    from forge.workflow.effect_runtime import JiraClient

logger = logging.getLogger(__name__)


def _draft_item_count(draft: object) -> int:
    if isinstance(draft, Mapping):
        return len(draft.get("items", []))
    return len(getattr(draft, "items", []))


def task_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for human to review generated Tasks before implementation.

    This gate pauses the workflow after task generation, allowing humans to:
    - Review the generated tasks for accuracy and completeness
    - Modify tasks manually in Jira if needed
    - Approve when ready for AI implementation

    The workflow resumes when:
    - Label changes to forge:task-approved -> proceed to implementation
    - Comment starting with ! -> regenerate tasks

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True, or error state if no tasks.
    """
    ticket_key = state["ticket_key"]
    task_keys = state.get("task_keys", [])
    draft = state.get("tasks_draft")
    task_count = len(task_keys) or _draft_item_count(draft)

    request = project_approval(state, "task", item_count=task_count)
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "task_approval_gate", "generate_tasks")
    logger.info(
        f"Task approval gate: pausing workflow for {ticket_key} "
        f"({task_count} Tasks pending implementation approval)"
    )

    return update_state_timestamp({**state, **updates})


def route_task_approval(state: WorkflowState) -> str:
    """Route based on task approval status.

    Routing logic:
    - Question (Q&A mode) -> answer_question
    - YOLO mode enabled -> auto-approve without human input
    - ! comment on specific Task ticket -> update_single_task
    - ! comment on Feature ticket -> regenerate_all_tasks
    - Label changed to approved -> task_router
    - Still paused -> END (wait for webhook)

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    ticket_key = state["ticket_key"]

    task_keys = state.get("task_keys") or []
    draft = state.get("tasks_draft")
    item_count = len(task_keys) or _draft_item_count(draft)
    outcome = run_approval_station(project_approval(state, "task", item_count=item_count))
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {ticket_key}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving tasks for {ticket_key}")
        record_approval("task")
        return "provision_tasks"

    # Check if revision requested (! feedback comment added)
    if disposition is ApprovalDisposition.REVISION:
        feedback = state.get("feedback_comment", "")
        current_task = state.get("current_task_key")
        current_epic = state.get("current_epic_key")

        if outcome.output.revision_scope == "task":
            # Single Task update - comment was on a specific Task
            logger.info(f"Single Task revision requested for {current_task}")
            record_revision_requested("task")
            return "update_single_task"
        elif outcome.output.revision_scope == "epic":
            # Epic-level regeneration - comment was on a specific Epic
            logger.info(f"Epic Task regeneration requested for {current_epic} on {ticket_key}")
            record_revision_requested("task")
            return "regenerate_epic_tasks"
        else:
            # Feature-level regeneration - comment was on Feature
            logger.info(f"Full Task regeneration requested for {ticket_key}: {feedback[:100]}...")
            record_revision_requested("task")
            return "regenerate_all_tasks"

    # Check if still paused - END and wait for approval webhook
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Task approval gate: workflow paused for {ticket_key}, "
            "waiting for forge:task-approved label"
        )
        return END

    return END


async def provision_tasks(state: WorkflowState) -> WorkflowState:
    """Create approved Task drafts before implementation routing begins."""
    if state.get("task_keys"):
        return state

    from forge.workflow.effect_runtime import JiraClient

    jira = JiraClient()
    try:
        task_keys, tasks_by_repo = await provision_tasks_from_draft(state, jira)
        return {**state, "task_keys": task_keys, "tasks_by_repo": tasks_by_repo}
    finally:
        await jira.close()


async def provision_tasks_from_draft(
    state: WorkflowState, jira: "JiraClient"
) -> tuple[list[str], dict[str, list[str]]]:
    """Materialize the approved workflow-state draft as Jira Tasks."""
    from forge.config import get_settings
    from forge.integrations.jira.client import MissingProjectConfig
    from forge.models.draft import ForgeDecompositionDraft
    from forge.models.workflow import ForgeLabel

    ticket_key = state["ticket_key"]
    existing = await jira.search_issues(
        f'labels = "forge:parent:{ticket_key}" AND issuetype = Task'
    )
    if existing:
        by_repo: dict[str, list[str]] = {}
        for issue in existing:
            repo = next(
                (
                    label.removeprefix("repo:")
                    for label in issue.labels
                    if label.startswith("repo:")
                ),
                "unknown",
            )
            by_repo.setdefault(repo, []).append(issue.key)
        return [issue.key for issue in existing], by_repo

    raw = state.get("tasks_draft")
    if not raw:
        raise ValueError(f"Approved tasks_draft not found for {ticket_key}")
    draft = ForgeDecompositionDraft.model_validate(raw) if isinstance(raw, dict) else raw
    project_key = (await jira.get_issue(ticket_key)).project_key
    settings = get_settings()
    task_keys: list[str] = []
    by_repo: dict[str, list[str]] = {}
    for item in draft.items:
        if item.excluded:
            continue
        repo = item.repo
        if not repo or "/" not in repo:
            try:
                repo = await jira.get_project_default_repo(project_key)
            except MissingProjectConfig:
                repo = (
                    settings.github_default_repo
                    if not settings.forge_require_project_config
                    else ""
                )
        parent_key = item.epic_key or next(iter(state.get("epic_keys") or []), None)
        labels = [ForgeLabel.FORGE_MANAGED.value, f"forge:parent:{ticket_key}"]
        if repo and "/" in repo:
            labels.append(f"repo:{repo}")
        task_key = await jira.create_task(
            project_key=project_key,
            summary=item.summary,
            description=item.description,
            parent_key=parent_key,
            labels=labels,
        )
        try:
            await jira.resolve_and_maybe_assign_tier(task_key)
        except Exception as exc:
            logger.warning("Failed to assign model tier to Task %s: %s", task_key, exc)
        task_keys.append(task_key)
        if repo and "/" in repo:
            by_repo.setdefault(repo, []).append(task_key)
    return task_keys, by_repo
