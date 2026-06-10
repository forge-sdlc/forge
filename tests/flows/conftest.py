"""Flow test fixtures - workflow state machine testing."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from forge.config import Settings
from forge.models.workflow import ForgeLabel
from forge.workflow.feature.state import FeatureState as WorkflowState
from tests.fixtures.workflow_states import (
    STATE_BUG_NEW,
    STATE_CI_FAILED,
    STATE_IMPLEMENTING,
    STATE_NEW_FEATURE,
    STATE_PLAN_PENDING,
    STATE_PRD_PENDING,
    STATE_RCA_PENDING,
    STATE_REVIEW_PENDING,
    STATE_SPEC_PENDING,
)


@pytest.fixture
def mock_settings() -> Settings:
    """Create mock settings for flow tests."""
    return Settings(
        redis_url="redis://localhost:6379/2",  # Use different DB for flow tests
        jira_base_url="https://test.atlassian.net",
        jira_api_token="test-token",
        jira_user_email="test@example.com",
        jira_webhook_secret="test-webhook-secret",
        github_token="test-github-token",
        github_webhook_secret="test-github-webhook-secret",
        anthropic_api_key="test-anthropic-key",
    )


@pytest.fixture
def mock_jira_client() -> MagicMock:
    """Create a mock Jira client with label tracking."""
    from forge.integrations.jira.models import JiraIssue

    mock = MagicMock()
    # Track labels for state verification
    mock._labels = ["forge:managed"]

    def get_issue(issue_key: str):
        return JiraIssue(
            key=issue_key,
            id="10001",
            summary="Test Issue",
            description="Test description",
            status="New",
            issue_type="Feature",
            labels=list(mock._labels),
            project_key="TEST",
        )

    def set_workflow_label(issue_key: str, new_label: ForgeLabel, **kwargs):
        # Remove old forge: labels except managed
        mock._labels = [
            l for l in mock._labels
            if not l.startswith("forge:") or l == "forge:managed"
        ]
        mock._labels.append(new_label.value)

    def add_labels(issue_key: str, labels: list[str]):
        mock._labels.extend(labels)

    def remove_labels(issue_key: str, labels: list[str]):
        mock._labels = [l for l in mock._labels if l not in labels]

    mock.get_issue = AsyncMock(side_effect=get_issue)
    mock.update_description = AsyncMock()
    mock.transition_issue = AsyncMock()
    mock.create_epic = AsyncMock(side_effect=lambda *args, **kwargs: f"TEST-{100 + len(mock.create_epic.call_args_list)}")
    mock.create_task = AsyncMock(side_effect=lambda *args, **kwargs: f"TEST-{200 + len(mock.create_task.call_args_list)}")
    mock.delete_issue = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.get_comments = AsyncMock(return_value=[])
    mock.get_labels = AsyncMock(side_effect=lambda issue_key: list(mock._labels))
    mock.add_labels = AsyncMock(side_effect=add_labels)
    mock.remove_labels = AsyncMock(side_effect=remove_labels)
    mock.set_workflow_label = AsyncMock(side_effect=set_workflow_label)
    mock.add_structured_comment = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_github_client() -> MagicMock:
    """Create a mock GitHub client for flow tests."""
    mock = MagicMock()
    mock._pr_count = 0

    def create_pull_request(*args, **kwargs):
        mock._pr_count += 1
        return {
            "number": 40 + mock._pr_count,
            "html_url": f"https://github.com/org/repo/pull/{40 + mock._pr_count}",
        }

    mock.create_pull_request = AsyncMock(side_effect=create_pull_request)
    mock.get_pull_request = AsyncMock(
        return_value={"number": 42, "state": "open", "mergeable": True}
    )
    mock.get_check_runs = AsyncMock(
        return_value=[{"name": "CI", "conclusion": "success", "status": "completed"}]
    )
    mock.create_issue_comment = AsyncMock()
    mock.merge_pull_request = AsyncMock()
    mock.get_workflow_run_logs = AsyncMock(return_value="Test logs")
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_forge_agent() -> MagicMock:
    """Create a mock ForgeAgent for flow tests."""
    mock = MagicMock()
    mock.generate_prd = AsyncMock(
        return_value="# PRD\n\nGenerated PRD content."
    )
    mock.generate_spec = AsyncMock(
        return_value="# Spec\n\nGenerated spec content."
    )
    mock.generate_epics = AsyncMock(
        return_value=[
            {"summary": "Epic 1", "plan": "Plan 1", "repo": "org/backend"},
            {"summary": "Epic 2", "plan": "Plan 2", "repo": "org/frontend"},
        ]
    )
    mock.regenerate_with_feedback = AsyncMock(
        return_value="# Revised\n\nRevised content."
    )
    mock.run_task = AsyncMock(return_value="Implementation complete.")
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_workspace_manager() -> MagicMock:
    """Create a mock WorkspaceManager for flow tests."""
    mock = MagicMock()
    mock.create_workspace = AsyncMock(return_value="/tmp/forge-workspace-test")
    mock.destroy_workspace = AsyncMock()
    mock.cleanup_stale_workspaces = AsyncMock()
    return mock


@pytest.fixture
def new_feature_state() -> WorkflowState:
    """Fresh feature state."""
    return deepcopy(STATE_NEW_FEATURE)


@pytest.fixture
def prd_pending_state() -> WorkflowState:
    """State at PRD approval gate."""
    return deepcopy(STATE_PRD_PENDING)


@pytest.fixture
def spec_pending_state() -> WorkflowState:
    """State at Spec approval gate."""
    return deepcopy(STATE_SPEC_PENDING)


@pytest.fixture
def plan_pending_state() -> WorkflowState:
    """State at Plan approval gate."""
    return deepcopy(STATE_PLAN_PENDING)


@pytest.fixture
def implementing_state() -> WorkflowState:
    """State during implementation."""
    return deepcopy(STATE_IMPLEMENTING)


@pytest.fixture
def ci_failed_state() -> WorkflowState:
    """State with CI failure."""
    return deepcopy(STATE_CI_FAILED)


@pytest.fixture
def review_pending_state() -> WorkflowState:
    """State awaiting human review."""
    return deepcopy(STATE_REVIEW_PENDING)


@pytest.fixture
def bug_new_state() -> WorkflowState:
    """Fresh bug state."""
    return deepcopy(STATE_BUG_NEW)


@pytest.fixture
def rca_pending_state() -> WorkflowState:
    """Bug state at RCA approval gate."""
    return deepcopy(STATE_RCA_PENDING)


class WorkflowTestHelper:
    """Helper class for workflow testing."""

    def __init__(
        self,
        mock_jira: MagicMock,
        mock_github: MagicMock,
        mock_agent: MagicMock,
    ):
        self.mock_jira = mock_jira
        self.mock_github = mock_github
        self.mock_agent = mock_agent

    def get_current_labels(self) -> list[str]:
        """Get current Jira labels from mock."""
        return list(self.mock_jira._labels)

    def has_label(self, label: ForgeLabel | str) -> bool:
        """Check if a label is present."""
        label_str = label.value if isinstance(label, ForgeLabel) else label
        return label_str in self.mock_jira._labels

    def simulate_approval(self, current_label: ForgeLabel, approved_label: ForgeLabel):
        """Simulate user approving by changing labels."""
        self.mock_jira._labels = [
            l for l in self.mock_jira._labels
            if l != current_label.value
        ]
        self.mock_jira._labels.append(approved_label.value)

    def simulate_rejection_with_comment(self, comment: str):
        """Simulate user rejecting with feedback comment."""
        from forge.integrations.jira.models import JiraComment

        self.mock_jira.get_comments = AsyncMock(
            return_value=[
                JiraComment(
                    id="10100",
                    author="PM User",
                    body=comment,
                    created="2024-03-30T10:10:00.000+0000",
                )
            ]
        )

    def set_ci_result(self, conclusion: str):
        """Set CI check result."""
        self.mock_github.get_check_runs = AsyncMock(
            return_value=[
                {"name": "CI", "conclusion": conclusion, "status": "completed"}
            ]
        )


@pytest.fixture
def workflow_helper(
    mock_jira_client: MagicMock,
    mock_github_client: MagicMock,
    mock_forge_agent: MagicMock,
) -> WorkflowTestHelper:
    """Create a workflow test helper."""
    return WorkflowTestHelper(
        mock_jira=mock_jira_client,
        mock_github=mock_github_client,
        mock_agent=mock_forge_agent,
    )
