"""Unit tests for informational PR command comments on PR creation."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    Provider,
    RepositoryRef,
    WriteTarget,
)
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.pr_creation import create_pull_request


def _repo_ref(identifier: str) -> RepositoryRef:
    return RepositoryRef(
        id=identifier,
        provider=Provider.GITHUB,
        connection="c",
        namespace=identifier,
        default_branch="main",
        change_request_mode="fork",
    )


def create_mock_adapter(
    pr_number=123, pr_url="https://github.com/owner/repo/pull/123", is_new_pr=True
):
    """Create a mock SourceControlProvider adapter with configurable PR data."""
    adapter = AsyncMock()
    adapter.ensure_write_target = AsyncMock(
        return_value=WriteTarget(
            clone_url="",
            push_remote_name="origin",
            head_ref="",
            base_branch="main",
            fork_owner="fork-owner",
            fork_repo="repo",
        )
    )
    adapter.create_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="c", repository_id="owner/repo", native_id=pr_number
            ),
            url=pr_url,
            title="t",
            body="b",
            state=ChangeRequestState.OPEN,
            source_branch="f",
            target_branch="main",
            created=is_new_pr,
        )
    )
    adapter.get_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="c", repository_id="owner/repo", native_id=pr_number
            ),
            url=pr_url,
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="f",
            target_branch="main",
        )
    )
    adapter.update_change_request = AsyncMock()
    adapter.create_comment = AsyncMock()
    return adapter


def create_mock_jira_client():
    """Create a mock JiraClient."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.create_remote_link = AsyncMock()
    mock.get_issue = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.is_repo_draft = AsyncMock(return_value=False)

    # Mock issue with summary
    mock_issue = MagicMock()
    mock_issue.summary = "Test feature"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_git_operations():
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.add_fork_remote = MagicMock()
    mock.push_to_fork = MagicMock()

    # Mock git log for PR body generation
    mock_result = MagicMock()
    mock_result.stdout = "abc123 Test commit\n\nTest commit body"
    mock._run_git = MagicMock(return_value=mock_result)

    return mock


def create_mock_workspace():
    """Create a mock Workspace."""
    mock = MagicMock()
    mock.path = Path("/tmp/test-workspace")
    return mock


@pytest.fixture(autouse=True)
def mock_external_pr_creation_side_effects():
    """Keep PR creation tests from reaching agent or Redis-backed helpers."""
    with (
        patch(
            "forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
            new_callable=AsyncMock,
            return_value="Generated PR body",
        ),
        patch("forge.workflow.nodes.pr_creation.set_pr_ticket_index", new_callable=AsyncMock),
    ):
        yield


def _patch_adapter(adapter):
    return patch(
        "forge.workflow.nodes.pr_creation.get_adapter",
        return_value=(_repo_ref("owner/repo"), adapter),
    )


class TestPRInformationalComment:
    """Test cases for the informational PR command comment on PR creation."""

    @pytest.mark.asyncio
    async def test_posts_comment_on_new_pr(self):
        """Should post an informational comment when a new PR is created."""
        mock_adapter = create_mock_adapter(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=True
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            _patch_adapter(mock_adapter),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify comment was posted
        mock_adapter.create_comment.assert_awaited_once()
        call_args = mock_adapter.create_comment.call_args[0]
        assert call_args[1].native_id == 456
        assert "/forge rebase" in call_args[2]
        assert "/forge skip-gate" in call_args[2]
        assert "/forge unskip-gate" in call_args[2]

    @pytest.mark.asyncio
    async def test_does_not_post_comment_on_existing_pr(self):
        """Should NOT post an informational comment when an existing PR is returned."""
        mock_adapter = create_mock_adapter(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=False
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            _patch_adapter(mock_adapter),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify comment was NOT posted
        mock_adapter.create_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_failure_ignored(self):
        """Should successfully finish PR creation even if posting comment fails."""
        mock_adapter = create_mock_adapter(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456", is_new_pr=True
        )
        # Make the comment creation raise an exception
        mock_adapter.create_comment.side_effect = Exception("GitHub API Error")
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test-branch"}

        with (
            _patch_adapter(mock_adapter),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            result = await create_pull_request(state)

        # Ensure node completed and returned correct state
        assert result["current_pr_number"] == 456
        assert result["current_pr_url"] == "https://github.com/owner/repo/pull/456"
        assert result["current_node"] == "teardown_workspace"
