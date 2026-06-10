"""Unit tests for workflow models."""


from forge.models.workflow import (
    ForgeLabel,
    JiraStatus,
    TicketType,
    WorkspaceStatus,
    get_workflow_phase,
    is_forge_managed,
)


class TestForgeLabel:
    """Tests for ForgeLabel enum."""

    def test_prd_workflow_labels_exist(self):
        """Verify PRD workflow labels are defined."""
        assert ForgeLabel.PRD_DRAFTING.value == "forge:prd-drafting"
        assert ForgeLabel.PRD_PENDING.value == "forge:prd-pending"
        assert ForgeLabel.PRD_APPROVED.value == "forge:prd-approved"
        assert ForgeLabel.PRD_REJECTED.value == "forge:prd-rejected"

    def test_spec_workflow_labels_exist(self):
        """Verify Spec workflow labels are defined."""
        assert ForgeLabel.SPEC_DRAFTING.value == "forge:spec-drafting"
        assert ForgeLabel.SPEC_PENDING.value == "forge:spec-pending"
        assert ForgeLabel.SPEC_APPROVED.value == "forge:spec-approved"
        assert ForgeLabel.SPEC_REJECTED.value == "forge:spec-rejected"

    def test_plan_workflow_labels_exist(self):
        """Verify Plan workflow labels are defined."""
        assert ForgeLabel.PLAN_DRAFTING.value == "forge:plan-drafting"
        assert ForgeLabel.PLAN_PENDING.value == "forge:plan-pending"
        assert ForgeLabel.PLAN_APPROVED.value == "forge:plan-approved"
        assert ForgeLabel.PLAN_REJECTED.value == "forge:plan-rejected"

    def test_task_workflow_labels_exist(self):
        """Verify Task workflow labels are defined."""
        assert ForgeLabel.TASK_GENERATED.value == "forge:task-generated"
        assert ForgeLabel.TASK_IMPLEMENTING.value == "forge:implementing"
        assert ForgeLabel.TASK_PR_CREATED.value == "forge:pr-created"
        assert ForgeLabel.TASK_CI_PENDING.value == "forge:ci-pending"
        assert ForgeLabel.TASK_CI_FAILED.value == "forge:ci-failed"
        assert ForgeLabel.TASK_REVIEW_PENDING.value == "forge:review-pending"
        assert ForgeLabel.TASK_REVIEW_APPROVED.value == "forge:review-approved"

    def test_bug_workflow_labels_exist(self):
        """Verify Bug workflow labels are defined."""
        assert ForgeLabel.RCA_DRAFTING.value == "forge:rca-drafting"
        assert ForgeLabel.RCA_PENDING.value == "forge:rca-pending"
        assert ForgeLabel.RCA_APPROVED.value == "forge:rca-approved"

    def test_triage_pending_label_value(self):
        """ForgeLabel.TRIAGE_PENDING == 'forge:triage-pending'."""
        assert ForgeLabel.TRIAGE_PENDING.value == "forge:triage-pending"

    def test_plan_pending_label_value(self):
        """ForgeLabel.PLAN_PENDING already exists with correct value."""
        assert ForgeLabel.PLAN_PENDING.value == "forge:plan-pending"

    def test_plan_approved_label_value(self):
        """ForgeLabel.PLAN_APPROVED already exists with correct value."""
        assert ForgeLabel.PLAN_APPROVED.value == "forge:plan-approved"

    def test_general_labels_exist(self):
        """Verify general labels are defined."""
        assert ForgeLabel.FORGE_MANAGED.value == "forge:managed"
        assert ForgeLabel.BLOCKED.value == "forge:blocked"


class TestJiraStatus:
    """Tests for JiraStatus enum."""

    def test_standard_statuses_exist(self):
        """Verify standard Jira statuses are defined."""
        assert JiraStatus.NEW.value == "New"
        assert JiraStatus.REFINEMENT.value == "Refinement"
        assert JiraStatus.IN_PROGRESS.value == "In Progress"
        assert JiraStatus.CLOSED.value == "Closed"


class TestTicketType:
    """Tests for TicketType enum."""

    def test_ticket_types_exist(self):
        """Verify ticket types are defined."""
        assert TicketType.FEATURE.value == "Feature"
        assert TicketType.EPIC.value == "Epic"
        assert TicketType.TASK.value == "Task"
        assert TicketType.BUG.value == "Bug"
        assert TicketType.STORY.value == "Story"


class TestWorkspaceStatus:
    """Tests for WorkspaceStatus enum."""

    def test_workspace_statuses_exist(self):
        """Verify workspace statuses are defined."""
        assert WorkspaceStatus.CREATED.value == "created"
        assert WorkspaceStatus.ACTIVE.value == "active"
        assert WorkspaceStatus.COMMITTED.value == "committed"
        assert WorkspaceStatus.DESTROYED.value == "destroyed"


class TestGetWorkflowPhase:
    """Tests for get_workflow_phase function."""

    def test_no_labels_returns_none(self):
        """Empty labels returns None."""
        assert get_workflow_phase([]) is None

    def test_non_forge_labels_returns_none(self):
        """Non-forge labels returns None."""
        assert get_workflow_phase(["bug", "priority:high"]) is None

    def test_missing_managed_label_returns_none(self):
        """Forge labels without managed label returns None."""
        labels = ["forge:prd-pending"]
        assert get_workflow_phase(labels) is None

    def test_prd_drafting_phase(self):
        """PRD drafting label returns correct phase."""
        labels = ["forge:managed", "forge:prd-drafting"]
        assert get_workflow_phase(labels) == "prd_generation"

    def test_prd_pending_phase(self):
        """PRD pending label returns correct phase."""
        labels = ["forge:managed", "forge:prd-pending"]
        assert get_workflow_phase(labels) == "prd_approval"

    def test_prd_approved_phase(self):
        """PRD approved label returns correct phase."""
        labels = ["forge:managed", "forge:prd-approved"]
        assert get_workflow_phase(labels) == "spec_generation"

    def test_spec_drafting_phase(self):
        """Spec drafting label returns correct phase."""
        labels = ["forge:managed", "forge:spec-drafting"]
        assert get_workflow_phase(labels) == "spec_generation"

    def test_spec_pending_phase(self):
        """Spec pending label returns correct phase."""
        labels = ["forge:managed", "forge:spec-pending"]
        assert get_workflow_phase(labels) == "spec_approval"

    def test_spec_approved_phase(self):
        """Spec approved label returns correct phase."""
        labels = ["forge:managed", "forge:spec-approved"]
        assert get_workflow_phase(labels) == "epic_decomposition"

    def test_plan_pending_phase(self):
        """Plan pending label returns correct phase."""
        labels = ["forge:managed", "forge:plan-pending"]
        assert get_workflow_phase(labels) == "plan_approval"

    def test_plan_approved_phase(self):
        """Plan approved label returns correct phase."""
        labels = ["forge:managed", "forge:plan-approved"]
        assert get_workflow_phase(labels) == "task_generation"

    def test_implementing_phase(self):
        """Implementing label returns correct phase."""
        labels = ["forge:managed", "forge:implementing"]
        assert get_workflow_phase(labels) == "implementation"

    def test_ci_failed_phase(self):
        """CI failed label returns correct phase."""
        labels = ["forge:managed", "forge:ci-failed"]
        assert get_workflow_phase(labels) == "ci_fix"

    def test_review_pending_phase(self):
        """Review pending label returns correct phase."""
        labels = ["forge:managed", "forge:review-pending"]
        assert get_workflow_phase(labels) == "human_review"

    def test_rca_pending_phase(self):
        """RCA pending label returns correct phase for bugs."""
        labels = ["forge:managed", "forge:rca-pending"]
        assert get_workflow_phase(labels) == "rca_approval"

    def test_blocked_phase(self):
        """Blocked label returns blocked phase."""
        labels = ["forge:managed", "forge:blocked"]
        assert get_workflow_phase(labels) == "blocked"

    def test_priority_ordering(self):
        """Earlier phase labels take priority over later ones."""
        # If both prd-drafting and prd-pending exist, drafting takes priority
        labels = ["forge:managed", "forge:prd-pending", "forge:prd-drafting"]
        assert get_workflow_phase(labels) == "prd_generation"

    def test_managed_only_returns_unknown(self):
        """Only forge:managed label returns unknown."""
        labels = ["forge:managed"]
        assert get_workflow_phase(labels) == "unknown"


class TestIsForgeManaged:
    """Tests for is_forge_managed function."""

    def test_managed_label_present(self):
        """Returns True when forge:managed is present."""
        assert is_forge_managed(["forge:managed"]) is True

    def test_managed_label_with_others(self):
        """Returns True when forge:managed is among other labels."""
        assert is_forge_managed(["bug", "forge:managed", "priority:high"]) is True

    def test_managed_label_missing(self):
        """Returns False when forge:managed is missing."""
        assert is_forge_managed(["bug", "priority:high"]) is False

    def test_empty_labels(self):
        """Returns False for empty labels."""
        assert is_forge_managed([]) is False

    def test_other_forge_labels_without_managed(self):
        """Returns False with other forge labels but no managed."""
        assert is_forge_managed(["forge:prd-pending"]) is False
