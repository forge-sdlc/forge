"""Workflow state models and label-based workflow tracking.

Forge uses Jira labels (not custom statuses) to track workflow state.
This allows Forge to work with any Jira project without requiring
custom status configurations.

Label Workflow
==============

All Forge-managed issues must have the `forge:managed` label. Issues
without this label are ignored by the webhook handler.

PRD Workflow (Features):
    forge:managed      - Issue is managed by Forge
    forge:prd-drafting - PRD generation in progress
    forge:prd-pending  - PRD awaiting approval
    forge:prd-approved - PRD approved, ready for spec generation

Spec Workflow:
    forge:spec-drafting - Spec generation in progress
    forge:spec-pending  - Spec awaiting approval
    forge:spec-approved - Spec approved, ready for epic decomposition

Epic/Plan Workflow:
    forge:plan-drafting - Epic decomposition in progress
    forge:plan-pending  - Plan awaiting approval
    forge:plan-approved - Plan approved, ready for task generation

Task Workflow:
    forge:task-generated  - Tasks have been generated
    forge:implementing    - Code implementation in progress
    forge:pr-created      - Pull request created
    forge:ci-pending      - Waiting for CI/CD results
    forge:ci-failed       - CI/CD failed, attempting fix
    forge:review-pending  - Awaiting human review
    forge:review-approved - Review approved, ready to merge

Bug Workflow:
    forge:rca-drafting    - Root cause analysis in progress
    forge:rca-pending     - RCA awaiting approval
    forge:rca-approved    - RCA approved, ready for fix implementation
    forge:triage-pending  - Ticket awaiting reporter update with required fields

Error States:
    forge:blocked - Workflow blocked, requires manual intervention

Approval Flow
=============

To approve a generated artifact (PRD, Spec, Plan, RCA):
1. Review the attached artifact file ({ticket}-prd.md, {ticket}-spec.md, etc.)
2. Change the label from `forge:*-pending` to `forge:*-approved`
3. Forge will automatically proceed to the next workflow stage

To request revisions:
1. Add a comment with your feedback
2. Keep the `forge:*-pending` label
3. Forge will regenerate the artifact incorporating your feedback

Jira Status Mapping
===================

Forge is agnostic to Jira statuses, but recommends:
- New/Refinement: PRD and Spec drafting/approval phases
- In Progress: Implementation and CI/CD phases
- Closed: Workflow complete
"""

from enum import StrEnum


class JiraStatus(StrEnum):
    """Standard Jira statuses used for issue transitions.

    These are actual Jira workflow statuses (not Forge labels).
    Available statuses depend on the Jira project configuration.
    """

    NEW = "New"
    REFINEMENT = "Refinement"
    IN_PROGRESS = "In Progress"
    CLOSED = "Closed"


class ForgeLabel(StrEnum):
    """Labels used to track Forge workflow state.

    These labels are added/removed to track progress through the
    SDLC workflow without requiring custom Jira statuses.
    """

    # PRD workflow
    PRD_DRAFTING = "forge:prd-drafting"
    PRD_PENDING = "forge:prd-pending"
    PRD_APPROVED = "forge:prd-approved"
    PRD_REJECTED = "forge:prd-rejected"

    # Spec workflow
    SPEC_DRAFTING = "forge:spec-drafting"
    SPEC_PENDING = "forge:spec-pending"
    SPEC_APPROVED = "forge:spec-approved"
    SPEC_REJECTED = "forge:spec-rejected"

    # Epic/Plan workflow
    PLAN_DRAFTING = "forge:plan-drafting"
    PLAN_PENDING = "forge:plan-pending"
    PLAN_APPROVED = "forge:plan-approved"
    PLAN_REJECTED = "forge:plan-rejected"

    # Task workflow
    TASK_GENERATED = "forge:task-generated"
    TASK_PENDING = "forge:task-pending"  # Awaiting approval to start implementation
    TASK_APPROVED = "forge:task-approved"  # Approved for implementation
    TASK_IMPLEMENTING = "forge:implementing"
    TASK_PR_CREATED = "forge:pr-created"
    TASK_CI_PENDING = "forge:ci-pending"
    TASK_CI_FAILED = "forge:ci-failed"
    TASK_REVIEW_PENDING = "forge:review-pending"
    TASK_REVIEW_APPROVED = "forge:review-approved"

    # Bug workflow
    RCA_DRAFTING = "forge:rca-drafting"
    RCA_PENDING = "forge:rca-pending"
    RCA_APPROVED = "forge:rca-approved"
    TRIAGE_PENDING = "forge:triage-pending"

    # General
    FORGE_MANAGED = "forge:managed"
    BLOCKED = "forge:blocked"
    RETRY = "forge:retry"  # Add to trigger retry of current stage


class TicketType(StrEnum):
    """Jira issue types supported by the orchestrator."""

    FEATURE = "Feature"
    EPIC = "Epic"
    TASK = "Task"
    BUG = "Bug"
    STORY = "Story"  # Treated as Feature
    UNKNOWN = "Unknown"


class WorkspaceStatus(StrEnum):
    """Status values for ephemeral workspaces."""

    CREATED = "created"
    ACTIVE = "active"
    COMMITTED = "committed"
    DESTROYED = "destroyed"


def get_workflow_phase(labels: list[str]) -> str | None:
    """Determine workflow phase from Forge labels.

    Args:
        labels: List of Jira labels on the issue.

    Returns:
        Current workflow phase or None if not Forge-managed.
    """
    forge_labels = [label for label in labels if label.startswith("forge:")]

    if not forge_labels:
        return None

    if ForgeLabel.FORGE_MANAGED.value not in labels:
        return None

    # Priority order for phase detection (most specific first)
    phase_priority = [
        (ForgeLabel.PRD_DRAFTING.value, "prd_generation"),
        (ForgeLabel.PRD_PENDING.value, "prd_approval"),
        (ForgeLabel.PRD_APPROVED.value, "spec_generation"),
        (ForgeLabel.SPEC_DRAFTING.value, "spec_generation"),
        (ForgeLabel.SPEC_PENDING.value, "spec_approval"),
        (ForgeLabel.SPEC_APPROVED.value, "epic_decomposition"),
        (ForgeLabel.PLAN_DRAFTING.value, "epic_decomposition"),
        (ForgeLabel.PLAN_PENDING.value, "plan_approval"),
        (ForgeLabel.PLAN_APPROVED.value, "task_generation"),
        (ForgeLabel.TASK_GENERATED.value, "task_routing"),
        (ForgeLabel.TASK_PENDING.value, "task_approval"),
        (ForgeLabel.TASK_APPROVED.value, "task_execution"),
        (ForgeLabel.TASK_IMPLEMENTING.value, "implementation"),
        (ForgeLabel.TASK_PR_CREATED.value, "pr_created"),
        (ForgeLabel.TASK_CI_PENDING.value, "ci_evaluation"),
        (ForgeLabel.TASK_CI_FAILED.value, "ci_fix"),
        (ForgeLabel.TASK_REVIEW_PENDING.value, "human_review"),
        (ForgeLabel.TASK_REVIEW_APPROVED.value, "complete"),
        (ForgeLabel.TRIAGE_PENDING.value, "triage_gate"),
        (ForgeLabel.RCA_DRAFTING.value, "rca_generation"),
        (ForgeLabel.RCA_PENDING.value, "rca_approval"),
        (ForgeLabel.RCA_APPROVED.value, "bug_fix"),
        (ForgeLabel.BLOCKED.value, "blocked"),
    ]

    for label, phase in phase_priority:
        if label in forge_labels:
            return phase

    return "unknown"


def is_forge_managed(labels: list[str]) -> bool:
    """Check if an issue is managed by Forge.

    Args:
        labels: List of Jira labels on the issue.

    Returns:
        True if the issue has the forge:managed label.
    """
    return ForgeLabel.FORGE_MANAGED.value in labels
