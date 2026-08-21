"""Unit tests for PR creation node - PR number extraction and handling."""

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


def create_mock_adapter(pr_number=123, pr_url="https://github.com/owner/repo/pull/123"):
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
            created=True,
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


def _patch_adapter(adapter, identifier="owner/repo"):
    return patch(
        "forge.workflow.nodes.pr_creation.get_adapter",
        return_value=(_repo_ref(identifier), adapter),
    )


class TestPRNumberExtractionSuccess:
    """Test cases for successful PR number extraction from GitHub API response."""

    @pytest.mark.asyncio
    async def test_pr_number_extracted_from_github_response(self):
        """Should extract PR number from GitHub API response and store in state."""
        mock_adapter = create_mock_adapter(
            pr_number=456, pr_url="https://github.com/owner/repo/pull/456"
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1", "TASK-2"]
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

        # Verify PR number is in state
        assert result["current_pr_number"] == 456

    @pytest.mark.asyncio
    async def test_pr_number_used_in_jira_remote_link(self):
        """Should use PR number in Jira remote link label when available."""
        mock_adapter = create_mock_adapter(pr_number=789)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-456",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify Jira remote link uses PR number
        mock_jira.create_remote_link.assert_called_once()
        call_args = mock_jira.create_remote_link.call_args
        assert call_args[0][2] == "PR #789"  # Third argument is the label

    @pytest.mark.asyncio
    async def test_pr_number_used_in_info_logging(self, caplog):
        """Should include PR number in info log message when available."""
        mock_adapter = create_mock_adapter(pr_number=999)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-789",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify info log includes PR number
        assert any(
            "Created PR #999:" in record.message
            for record in caplog.records
            if record.levelname == "INFO"
        )


class TestPRCreationDirectMode:
    """Direct-mode repos (no fork identity) must push to origin, not 'fork'."""

    @pytest.mark.asyncio
    async def test_direct_mode_pushes_to_origin_not_fork(self):
        mock_adapter = create_mock_adapter(pr_number=1)
        mock_adapter.ensure_write_target = AsyncMock(
            return_value=WriteTarget(
                clone_url="",
                push_remote_name="origin",
                head_ref="",
                base_branch="main",
                fork_owner=None,
                fork_repo=None,
            )
        )
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()
        mock_git.push = MagicMock()

        state = create_initial_feature_state(
            ticket_key="FEAT-DIRECT",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        mock_git.add_fork_remote.assert_not_called()
        mock_git.push_to_fork.assert_not_called()
        mock_git.push.assert_called_once_with(force=False)
        assert result["last_error"] is None


class TestPRNumberExtractionMissing:
    """Test cases for handling missing PR number in GitHub API response."""

    @pytest.mark.asyncio
    async def test_pr_number_none_when_unavailable(self):
        """Should set current_pr_number to None when PR number unavailable in API response."""
        # GitHub API returns response without 'number' field
        mock_adapter = create_mock_adapter(pr_number=None)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-111",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify PR number is None in state. With no number known, the record is
        # keyed by URL (f"{repo}:{url}") — see workflow.pr_state.
        assert result["current_pr_number"] is None
        url_key = f"owner/repo:{result['current_pr_url']}"
        assert result["pull_requests"][url_key]["number"] is None
        assert result["pull_requests"][url_key]["url"] == result["current_pr_url"]

    @pytest.mark.asyncio
    async def test_workflow_continues_when_pr_number_unavailable(self):
        """Should continue workflow successfully even when PR number unavailable."""
        mock_adapter = create_mock_adapter(pr_number=None)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-222",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify workflow completed successfully
        assert result["current_node"] == "teardown_workspace"
        assert result["last_error"] is None

        # Verify PR URL was still stored
        assert result["current_pr_url"] is not None
        assert len(result["pr_urls"]) > 0

    @pytest.mark.asyncio
    async def test_warning_logged_when_pr_number_unavailable(self, caplog):
        """Should log warning with diagnostic info when PR number unavailable."""
        pr_url = "https://github.com/owner/repo/pull/123"
        mock_adapter = create_mock_adapter(pr_number=None, pr_url=pr_url)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-333",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify warning log includes diagnostic information
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "PR number not available in GitHub API response" in record.message
            and "FEAT-333" in record.message
            and pr_url in record.message
            for record in warning_logs
        )

    @pytest.mark.asyncio
    async def test_generic_label_used_when_pr_number_unavailable(self):
        """Should use generic 'Pull Request' label in Jira remote link when PR number unavailable."""
        mock_adapter = create_mock_adapter(pr_number=None)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-444",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify Jira remote link uses generic label
        mock_jira.create_remote_link.assert_called_once()
        call_args = mock_jira.create_remote_link.call_args
        assert call_args[0][2] == "Pull Request"  # Generic label instead of "PR #123"

    @pytest.mark.asyncio
    async def test_info_log_indicates_number_unavailable(self, caplog):
        """Should log info message indicating PR number unavailable."""
        pr_url = "https://github.com/owner/repo/pull/456"
        mock_adapter = create_mock_adapter(pr_number=None, pr_url=pr_url)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-555",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify info log indicates number unavailable
        info_logs = [r for r in caplog.records if r.levelname == "INFO"]
        assert any(
            "Created PR (number unavailable):" in record.message and pr_url in record.message
            for record in info_logs
        )


class TestPRNumberExtractionEdgeCases:
    """Test cases for edge cases in PR number handling."""

    @pytest.mark.asyncio
    async def test_pr_number_zero_handled_correctly(self):
        """Should handle PR number 0 (edge case) correctly without treating it as None."""
        # PR number 0 is technically valid (though rare) and should not be treated as missing
        mock_adapter = create_mock_adapter(pr_number=0)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-666",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify PR number 0 is stored (not treated as None/missing)
        assert result["current_pr_number"] == 0

        # Verify Jira remote link includes "PR #0"
        mock_jira.create_remote_link.assert_called_once()
        call_args = mock_jira.create_remote_link.call_args
        assert call_args[0][2] == "PR #0"

    @pytest.mark.asyncio
    async def test_pr_number_extracted_when_pr_url_missing(self):
        """Should extract PR number even when PR URL is missing from response."""
        # Edge case: API returns number but not html_url
        mock_adapter = create_mock_adapter(pr_number=111, pr_url="")
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-777",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

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

        # Verify PR number is still extracted
        assert result["current_pr_number"] == 111

    @pytest.mark.asyncio
    async def test_multiple_prs_each_have_own_pr_number(self):
        """Should handle multiple PR creations with different PR numbers independently."""
        # This tests that pr_number is properly isolated per PR creation
        mock_adapter_1 = create_mock_adapter(pr_number=100)
        mock_jira = create_mock_jira_client()
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-888",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

        with (
            _patch_adapter(mock_adapter_1),
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
            result_1 = await create_pull_request(state)

        # Verify first PR has correct number
        assert result_1["current_pr_number"] == 100
        assert result_1["pull_requests"]["owner/repo:100"]["number"] == 100

        # Simulate second PR creation with different number
        mock_adapter_2 = create_mock_adapter(
            pr_number=200, pr_url="https://github.com/owner/other/pull/200"
        )
        result_1["current_repo"] = "owner/other"

        with (
            _patch_adapter(mock_adapter_2, identifier="owner/other"),
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
            result_2 = await create_pull_request(result_1)

        # Verify second PR has correct number; each PR occupies its own slot.
        assert result_2["current_pr_number"] == 200
        assert result_2["pull_requests"]["owner/repo:100"]["number"] == 100
        assert result_2["pull_requests"]["owner/other:200"]["number"] == 200
