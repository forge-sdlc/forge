"""Plan approval gate for human-in-the-loop review of Epic decomposition.

The plan approval workflow uses labels:
- forge:plan-pending  - Plan awaiting approval
- forge:plan-approved - Plan approved (triggers task generation)

To approve: Change label from forge:plan-pending to forge:plan-approved
To request revision: Add a comment starting with ! (keep forge:plan-pending)
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


def plan_approval_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow for Tech Lead to review Epic decomposition and plans.

    This gate pauses the workflow until a human approves or rejects
    the generated Epics and their implementation plans. The workflow resumes when:
    - Label changes to forge:plan-approved -> proceed to task generation
    - Comment starting with ! -> regenerate Epics with feedback

    Args:
        state: Current workflow state.

    Returns:
        State with is_paused=True, or error state if no epics.
    """
    ticket_key = state["ticket_key"]
    epic_keys = state.get("epic_keys", [])
    draft = state.get("plan_draft")
    epic_count = len(epic_keys) or _draft_item_count(draft)

    request = project_approval(state, "plan", item_count=epic_count)
    outcome = run_approval_station(request)
    updates = reduce_approval_gate(state, request, outcome, "plan_approval_gate", "decompose_epics")
    logger.info(f"Plan approval gate: pausing workflow for {ticket_key} ({epic_count} Epics)")

    return update_state_timestamp({**state, **updates})


def route_plan_approval(state: WorkflowState) -> str:
    """Route based on plan approval status.

    Args:
        state: Current workflow state.

    Returns:
        Next node name or END.
    """
    epic_keys = state.get("epic_keys") or []
    draft = state.get("plan_draft")
    item_count = len(epic_keys) or _draft_item_count(draft)
    outcome = run_approval_station(project_approval(state, "plan", item_count=item_count))
    assert outcome.output is not None
    disposition = outcome.output.disposition
    if disposition is ApprovalDisposition.QUESTION:
        logger.info(f"Q&A mode: routing to answer_question for {state['ticket_key']}")
        return "answer_question"

    # YOLO mode: auto-approve without human input
    if disposition is ApprovalDisposition.APPROVED:
        logger.info(f"YOLO mode: auto-approving plan for {state['ticket_key']}")
        record_approval("plan")
        return "provision_epics"

    # Check if revision requested
    if disposition is ApprovalDisposition.REVISION:
        if outcome.output.revision_scope in {"item", "epic"}:
            # Single Epic update
            logger.info("Single Epic revision requested for %s", state.get("current_epic_key"))
            record_revision_requested("plan")
            return "update_single_epic"
        else:
            # Feature-level regeneration
            logger.info(f"Full Epic regeneration requested for {state['ticket_key']}")
            record_revision_requested("plan")
            return "regenerate_all_epics"

    # Check if still paused - END and wait for approval webhook
    if disposition is ApprovalDisposition.WAITING:
        logger.info(
            f"Plan approval gate: workflow paused for {state['ticket_key']}, "
            "waiting for approval webhook"
        )
        return END

    return END


async def provision_epics(state: WorkflowState) -> WorkflowState:
    """Create approved Epic drafts before task planning begins."""
    if state.get("epic_keys"):
        return state

    from forge.workflow.effect_runtime import JiraClient

    jira = JiraClient()
    try:
        epic_keys = await provision_epics_from_draft(state, jira)
        return {**state, "epic_keys": epic_keys}
    finally:
        await jira.close()


async def provision_epics_from_draft(state: WorkflowState, jira: "JiraClient") -> list[str]:
    """Materialize the approved workflow-state draft as Jira Epics."""
    from forge.integrations.jira.client import JIRA_SUMMARY_MAX_LENGTH
    from forge.models.draft import ForgeDecompositionDraft
    from forge.models.workflow import ForgeLabel

    ticket_key = state["ticket_key"]
    existing = await jira.search_issues(
        f'labels = "forge:parent:{ticket_key}" AND issuetype = Epic',
        fields=["summary", "labels"],
        max_results=None,
    )

    raw = state.get("plan_draft")
    if not raw:
        raise ValueError(f"Approved plan_draft not found for {ticket_key}")
    draft = ForgeDecompositionDraft.model_validate(raw) if isinstance(raw, dict) else raw
    project_key = (await jira.get_issue(ticket_key)).project_key
    active_items = [item for item in draft.items if not item.excluded]
    marker_prefix = f"forge:epic-item:{ticket_key}:"
    claimed: set[str] = set()
    matches: dict[int, str] = {}

    # Stable per-item labels survive title edits and make a resumed provision
    # independent of Jira search ordering. Old Epics without markers are
    # matched by summary once and then upgraded to the stable label.
    for item in active_items:
        marker = f"{marker_prefix}{item.id}"
        candidates = [issue for issue in existing if marker in getattr(issue, "labels", [])]
        if len(candidates) > 1:
            raise ValueError(f"Epic marker {marker} matches multiple Jira issues")
        if candidates:
            matches[item.id] = candidates[0].key
            claimed.add(candidates[0].key)

    for item in active_items:
        if item.id in matches:
            continue
        summary = item.summary[:JIRA_SUMMARY_MAX_LENGTH]
        candidates = [
            issue
            for issue in existing
            if issue.key not in claimed
            and issue.summary == summary
            and not any(label.startswith(marker_prefix) for label in getattr(issue, "labels", []))
        ]
        if len(candidates) > 1:
            raise ValueError(f"Epic summary {summary!r} matches multiple unmarked Jira issues")
        if candidates:
            matches[item.id] = candidates[0].key
            claimed.add(candidates[0].key)
            await jira.add_labels(candidates[0].key, [f"{marker_prefix}{item.id}"])

    unclaimed = [
        issue.key
        for issue in existing
        if issue.key not in claimed
        and not any(label.startswith(marker_prefix) for label in getattr(issue, "labels", []))
    ]
    if unclaimed:
        raise ValueError(
            f"Cannot safely reconcile unmarked Epics for {ticket_key}: {', '.join(unclaimed)}"
        )

    epic_keys: list[str] = []
    for item in active_items:
        existing_key = matches.get(item.id)
        if existing_key:
            epic_keys.append(existing_key)
            continue
        labels = [
            ForgeLabel.FORGE_MANAGED.value,
            f"forge:parent:{ticket_key}",
            f"{marker_prefix}{item.id}",
        ]
        if item.repo and "/" in item.repo:
            labels.append(f"repo:{item.repo}")
        epic_keys.append(
            await jira.create_epic(
                project_key=project_key,
                summary=item.summary,
                description=item.description,
                parent_key=ticket_key,
                labels=labels,
            )
        )
    return epic_keys
