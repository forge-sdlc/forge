"""Unit test fixtures - fast, isolated tests with full mocking."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import Settings
from forge.models.workflow import TicketType
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state


@pytest.fixture
def mock_settings() -> Settings:
    """Create mock settings for unit tests."""
    return Settings(
        redis_url="redis://localhost:6379/0",
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
    """Create a mock Jira client with all methods mocked."""
    from forge.integrations.jira.models import JiraComment, JiraIssue

    mock = MagicMock()
    mock.get_issue = AsyncMock(
        return_value=JiraIssue(
            key="TEST-123",
            id="10001",
            summary="Test Issue",
            description="Test description",
            status="New",
            issue_type="Feature",
            labels=["forge:managed"],
            project_key="TEST",
        )
    )
    mock.update_description = AsyncMock()
    mock.update_custom_field = AsyncMock()
    mock.transition_issue = AsyncMock()
    mock.create_epic = AsyncMock(return_value="TEST-124")
    mock.create_task = AsyncMock(return_value="TEST-125")
    mock.delete_issue = AsyncMock()
    mock.add_comment = AsyncMock(
        return_value=JiraComment(
            id="10100",
            author="Test User",
            body="Test comment",
            created="2024-03-30T10:00:00.000+0000",
        )
    )
    mock.get_comments = AsyncMock(return_value=[])
    mock.get_labels = AsyncMock(return_value=["forge:managed"])
    mock.add_labels = AsyncMock()
    mock.remove_labels = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.add_structured_comment = AsyncMock()
    mock.get_structured_comment = AsyncMock(return_value=None)
    mock.add_attachment = AsyncMock(return_value={"id": "12345"})
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_github_client() -> MagicMock:
    """Create a mock GitHub client with all methods mocked."""
    mock = MagicMock()
    mock.create_pull_request = AsyncMock(
        return_value={
            "number": 42,
            "html_url": "https://github.com/org/repo/pull/42",
        }
    )
    mock.get_pull_request = AsyncMock(
        return_value={
            "number": 42,
            "state": "open",
            "title": "Test PR",
            "mergeable": True,
        }
    )
    mock.get_check_runs = AsyncMock(
        return_value=[
            {"name": "CI / Tests", "conclusion": "success", "status": "completed"}
        ]
    )
    mock.create_issue_comment = AsyncMock()
    mock.create_review = AsyncMock()
    mock.merge_pull_request = AsyncMock()
    mock.get_workflow_run_logs = AsyncMock(return_value="Test logs")
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_forge_agent() -> MagicMock:
    """Create a mock ForgeAgent with all methods mocked."""
    mock = MagicMock()
    mock.generate_prd = AsyncMock(
        return_value="# PRD\n\n## Overview\nGenerated PRD content."
    )
    mock.generate_spec = AsyncMock(
        return_value="# Spec\n\n## User Stories\nGenerated spec content."
    )
    mock.generate_epics = AsyncMock(
        return_value=[
            {"summary": "Epic 1: Backend Auth", "plan": "Implementation plan 1"},
            {"summary": "Epic 2: Frontend Auth", "plan": "Implementation plan 2"},
        ]
    )
    mock.regenerate_with_feedback = AsyncMock(
        return_value="# Revised Content\n\nContent incorporating feedback."
    )
    mock.run_task = AsyncMock(return_value="Task completed successfully.")
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def sample_workflow_state() -> WorkflowState:
    """Create a sample workflow state for testing."""
    return create_initial_state(
        thread_id="test-thread-001",
        ticket_key="TEST-123",
        ticket_type=TicketType.FEATURE,
    )


@pytest.fixture
def sample_prd_state() -> WorkflowState:
    """Create a workflow state at PRD approval stage."""
    state = create_initial_state(
        thread_id="test-thread-001",
        ticket_key="TEST-123",
        ticket_type=TicketType.FEATURE,
    )
    state["current_node"] = "prd_approval_gate"
    state["is_paused"] = True
    state["prd_content"] = "# PRD\n\nTest PRD content for approval."
    return state


@pytest.fixture
def sample_spec_state() -> WorkflowState:
    """Create a workflow state at Spec approval stage."""
    state = create_initial_state(
        thread_id="test-thread-001",
        ticket_key="TEST-123",
        ticket_type=TicketType.FEATURE,
    )
    state["current_node"] = "spec_approval_gate"
    state["is_paused"] = True
    state["prd_content"] = "# PRD\n\nApproved PRD content."
    state["spec_content"] = "# Spec\n\nTest spec content for approval."
    return state


@pytest.fixture
def sample_plan_state() -> WorkflowState:
    """Create a workflow state at Plan approval stage."""
    state = create_initial_state(
        thread_id="test-thread-001",
        ticket_key="TEST-123",
        ticket_type=TicketType.FEATURE,
    )
    state["current_node"] = "plan_approval_gate"
    state["is_paused"] = True
    state["prd_content"] = "# PRD\n\nApproved PRD content."
    state["spec_content"] = "# Spec\n\nApproved spec content."
    state["epic_keys"] = ["TEST-124", "TEST-125"]
    return state


@pytest.fixture
def patched_jira_client(mock_jira_client: MagicMock):
    """Patch JiraClient globally for tests."""
    with patch("forge.integrations.jira.client.JiraClient", return_value=mock_jira_client):
        yield mock_jira_client


@pytest.fixture
def patched_github_client(mock_github_client: MagicMock):
    """Patch GitHubClient globally for tests."""
    with patch("forge.integrations.github.client.GitHubClient", return_value=mock_github_client):
        yield mock_github_client


@pytest.fixture
def patched_forge_agent(mock_forge_agent: MagicMock):
    """Patch ForgeAgent globally for tests."""
    with patch("forge.integrations.agents.ForgeAgent", return_value=mock_forge_agent):
        yield mock_forge_agent
