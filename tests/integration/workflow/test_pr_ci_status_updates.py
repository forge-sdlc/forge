"""Integration tests for PR creation and CI attempt status updates.

This module contains end-to-end integration tests that verify:
- PR creation status comments with label transitions (TS-006)
- CI fix attempt status comments with correct attempt counts (TS-007)
- Fallback comment text when PR number unavailable (TS-014)

Test Coverage:
- TS-006: Verify PR creation posts comment with PR number and updates labels
- TS-007: Verify CI fix attempts post comments with correct attempt counts (1/3, 2/3, 3/3)
- TS-014: Verify comment uses fallback text when PR number unavailable
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.ci_evaluator import attempt_ci_fix, wait_for_ci_gate


def create_mock_jira_client():
    """Create a mock JiraClient with required methods for testing.
    
    Returns:
        MagicMock: Mock JiraClient with async methods for comment posting and label management.
    """
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.remove_labels = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    return mock


def create_mock_container_runner():
    """Create a mock ContainerRunner that succeeds.
    
    Returns:
        MagicMock: Mock ContainerRunner with async run method.
    """
    mock = MagicMock()
    mock.run = AsyncMock()
    return mock


def create_mock_github_client():
    """Create a mock GitHubClient.
    
    Returns:
        MagicMock: Mock GitHubClient with async close method.
    """
    mock = MagicMock()
    mock.close = AsyncMock()
    return mock


class TestPRCreationWithPRNumber:
    """TS-006: Verify PR creation posts comment with PR number and updates labels."""

    @pytest.mark.asyncio
    async def test_pr_creation_posts_comment_with_pr_number(self):
        """TS-006: Verify comment posted with PR number when available.
        
        This test ensures that when a PR is created successfully with a valid
        PR number, the status comment includes the PR number in the expected format.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify status comment posted with PR number
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-200"
        assert comment_call[0][1] == "🚀 Pull request #123 created and submitted. Waiting for CI checks to complete."

        # Verify workflow paused
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_pr_creation_removes_implementing_label(self):
        """TS-006: Verify forge:implementing label removed from feature ticket.
        
        This test ensures the label transition removes the implementing label
        when PR creation occurs.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:implementing label removed
        assert mock_jira.remove_labels.call_count == 1
        remove_call = mock_jira.remove_labels.call_args
        assert remove_call[0][0] == "FEAT-200"
        assert "forge:implementing" in remove_call[0][1]

    @pytest.mark.asyncio
    async def test_pr_creation_adds_ci_pending_label(self):
        """TS-006: Verify forge:ci-pending label added to feature ticket.
        
        This test ensures the label transition adds the ci-pending label
        when PR creation occurs.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:ci-pending label added
        assert mock_jira.set_workflow_label.call_count == 1
        label_call = mock_jira.set_workflow_label.call_args
        assert label_call[0][0] == "FEAT-200"
        # Check that it's the CI_PENDING label (value is "forge:ci-pending")
        from forge.models.workflow import ForgeLabel
        assert label_call[0][1] == ForgeLabel.TASK_CI_PENDING

    @pytest.mark.asyncio
    async def test_pr_creation_jira_client_properly_closed(self):
        """TS-006: Verify JiraClient properly closed after operations.
        
        This test ensures proper resource cleanup by verifying the JiraClient
        is closed in the finally block.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-200",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 123
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify JiraClient closed
        assert mock_jira.close.call_count == 1


class TestCIFixAttemptStatusComments:
    """TS-007: Verify CI fix attempts post comments with correct attempt counts."""

    @pytest.mark.asyncio
    async def test_first_attempt_posts_comment_with_1_of_3(self):
        """TS-007: Verify first CI fix attempt posts comment with '1/3' format.
        
        This test ensures the first fix attempt shows the correct count format.
        """
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-300",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["ci_failed_checks"] = [
            {
                "pr_url": "https://github.com/owner/test-repo/pull/1",
                "name": "test-check",
                "conclusion": "failure",
                "output": {},
                "log_url": "https://github.com/owner/test-repo/runs/1",
            }
        ]
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_attempt"] = 1
        state["max_attempts"] = 3

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.ci_evaluator.ContainerRunner", return_value=mock_runner):
                with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
                    with patch("forge.workflow.nodes.ci_evaluator.prepare_workspace") as mock_prepare:
                        mock_prepare.return_value = (Path("/tmp/test-workspace"), None)
                        with patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", AsyncMock()):
                            with patch("forge.workflow.nodes.ci_evaluator._collect_error_info", return_value="errors"):
                                with patch("forge.workflow.nodes.ci_evaluator.load_prompt", return_value="prompt"):
                                    with patch("pathlib.Path.mkdir"):
                                        with patch("pathlib.Path.write_text"):
                                            with patch("pathlib.Path.exists", return_value=False):
                                                await attempt_ci_fix(state)

        # Verify status comment posted with correct format "1/3"
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-300"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (1/3)."

        # Verify JiraClient closed
        assert mock_jira.close.call_count == 1

    @pytest.mark.asyncio
    async def test_second_attempt_posts_comment_with_2_of_3(self):
        """TS-007: Verify second CI fix attempt posts comment with '2/3' format.
        
        This test ensures the second fix attempt shows the correct count format.
        """
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-301",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["ci_failed_checks"] = [
            {
                "pr_url": "https://github.com/owner/test-repo/pull/1",
                "name": "test-check",
                "conclusion": "failure",
                "output": {},
                "log_url": "https://github.com/owner/test-repo/runs/1",
            }
        ]
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_attempt"] = 2
        state["max_attempts"] = 3

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.ci_evaluator.ContainerRunner", return_value=mock_runner):
                with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
                    with patch("forge.workflow.nodes.ci_evaluator.prepare_workspace") as mock_prepare:
                        mock_prepare.return_value = (Path("/tmp/test-workspace"), None)
                        with patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", AsyncMock()):
                            with patch("forge.workflow.nodes.ci_evaluator._collect_error_info", return_value="errors"):
                                with patch("forge.workflow.nodes.ci_evaluator.load_prompt", return_value="prompt"):
                                    with patch("pathlib.Path.mkdir"):
                                        with patch("pathlib.Path.write_text"):
                                            with patch("pathlib.Path.exists", return_value=False):
                                                await attempt_ci_fix(state)

        # Verify status comment posted with correct format "2/3"
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-301"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (2/3)."

    @pytest.mark.asyncio
    async def test_third_attempt_posts_comment_with_3_of_3(self):
        """TS-007: Verify third CI fix attempt posts comment with '3/3' format.
        
        This test ensures the final fix attempt shows the correct count format.
        """
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-302",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["ci_failed_checks"] = [
            {
                "pr_url": "https://github.com/owner/test-repo/pull/1",
                "name": "test-check",
                "conclusion": "failure",
                "output": {},
                "log_url": "https://github.com/owner/test-repo/runs/1",
            }
        ]
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_attempt"] = 3
        state["max_attempts"] = 3

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.ci_evaluator.ContainerRunner", return_value=mock_runner):
                with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
                    with patch("forge.workflow.nodes.ci_evaluator.prepare_workspace") as mock_prepare:
                        mock_prepare.return_value = (Path("/tmp/test-workspace"), None)
                        with patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", AsyncMock()):
                            with patch("forge.workflow.nodes.ci_evaluator._collect_error_info", return_value="errors"):
                                with patch("forge.workflow.nodes.ci_evaluator.load_prompt", return_value="prompt"):
                                    with patch("pathlib.Path.mkdir"):
                                        with patch("pathlib.Path.write_text"):
                                            with patch("pathlib.Path.exists", return_value=False):
                                                await attempt_ci_fix(state)

        # Verify status comment posted with correct format "3/3"
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-302"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (3/3)."


class TestPRCreationFallbackWithoutPRNumber:
    """TS-014: Verify comment uses fallback text when PR number unavailable."""

    @pytest.mark.asyncio
    async def test_pr_creation_posts_fallback_comment_without_pr_number(self):
        """TS-014: Verify fallback comment posted when PR number unavailable.
        
        This test ensures that when GitHub PR creation doesn't return a PR number,
        the fallback comment text is used instead of including a null/missing number.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-201",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        # PR number is None (unavailable)
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify fallback comment posted without PR number
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-201"
        assert comment_call[0][1] == "🚀 Pull request created and submitted. Waiting for CI checks to complete."

        # Verify workflow still paused correctly
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

    @pytest.mark.asyncio
    async def test_pr_creation_without_pr_number_still_updates_labels(self):
        """TS-014: Verify label transitions still occur when PR number unavailable.
        
        This test ensures that missing PR number doesn't prevent label transitions
        from occurring correctly.
        """
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-202",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        # PR number is None
        state["current_pr_number"] = None
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            await wait_for_ci_gate(state)

        # Verify forge:implementing label removed even without PR number
        assert mock_jira.remove_labels.call_count == 1
        remove_call = mock_jira.remove_labels.call_args
        assert remove_call[0][0] == "FEAT-202"
        assert "forge:implementing" in remove_call[0][1]

        # Verify forge:ci-pending label added even without PR number
        assert mock_jira.set_workflow_label.call_count == 1
        label_call = mock_jira.set_workflow_label.call_args
        assert label_call[0][0] == "FEAT-202"
        from forge.models.workflow import ForgeLabel
        assert label_call[0][1] == ForgeLabel.TASK_CI_PENDING


class TestErrorHandling:
    """Test error handling for Jira API failures."""

    @pytest.mark.asyncio
    async def test_workflow_continues_when_pr_comment_posting_fails(self, caplog):
        """Verify workflow continues when PR creation comment posting fails.
        
        This test ensures that Jira API failures don't block the workflow from
        continuing to the next state.
        """
        mock_jira = create_mock_jira_client()
        # Simulate Jira API failure
        mock_jira.add_comment.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-203",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 456
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite failure
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

        # Verify error was logged
        assert any("Failed to post status comment" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_workflow_continues_when_label_removal_fails(self, caplog):
        """Verify workflow continues when label removal fails.
        
        This test ensures that label API failures are properly suppressed and logged.
        """
        mock_jira = create_mock_jira_client()
        # Simulate label removal failure
        mock_jira.remove_labels.side_effect = Exception("Label removal error")

        state = create_initial_feature_state(
            ticket_key="FEAT-204",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["current_pr_number"] = 789
        state["ci_fix_attempts"] = 0  # Initial entry

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            result = await wait_for_ci_gate(state)

        # Verify workflow continues despite failure
        assert result["is_paused"] is True
        assert result["current_node"] == "wait_for_ci_gate"

        # Verify error was logged
        assert any("Failed to remove" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_workflow_continues_when_ci_attempt_comment_posting_fails(self, caplog):
        """Verify workflow continues when CI attempt comment posting fails.
        
        This test ensures that Jira failures during CI fix attempts don't block
        the workflow from continuing.
        """
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()
        # Simulate Jira API failure
        mock_jira.add_comment.side_effect = Exception("Jira API error")

        state = create_initial_feature_state(
            ticket_key="FEAT-305",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        state["ci_failed_checks"] = [
            {
                "pr_url": "https://github.com/owner/test-repo/pull/1",
                "name": "test-check",
                "conclusion": "failure",
                "output": {},
                "log_url": "https://github.com/owner/test-repo/runs/1",
            }
        ]
        state["workspace_path"] = "/tmp/test-workspace"
        state["current_attempt"] = 1
        state["max_attempts"] = 3

        with patch("forge.workflow.nodes.ci_evaluator.JiraClient", return_value=mock_jira):
            with patch("forge.workflow.nodes.ci_evaluator.ContainerRunner", return_value=mock_runner):
                with patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github):
                    with patch("forge.workflow.nodes.ci_evaluator.prepare_workspace") as mock_prepare:
                        mock_prepare.return_value = (Path("/tmp/test-workspace"), None)
                        with patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", AsyncMock()):
                            with patch("forge.workflow.nodes.ci_evaluator._collect_error_info", return_value="errors"):
                                with patch("forge.workflow.nodes.ci_evaluator.load_prompt", return_value="prompt"):
                                    with patch("pathlib.Path.mkdir"):
                                        with patch("pathlib.Path.write_text"):
                                            with patch("pathlib.Path.exists", return_value=False):
                                                result = await attempt_ci_fix(state)

        # Verify workflow continues despite failure
        assert "next_node" in result or "error" in result or result is not None

        # Verify error was logged
        assert any("Failed to post status comment" in record.message for record in caplog.records)
