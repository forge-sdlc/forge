"""Integration tests for CI fix attempt status comments (TS-007).

These tests verify that CI fix attempt status comments are posted correctly
to Jira feature tickets at the start of each CI fix attempt, displaying
current attempt and max attempts.

Test Coverage:
- TS-007: CI fix attempts post comments with correct counts
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.ci_evaluator import attempt_ci_fix


def create_mock_jira_client():
    """Create a mock JiraClient with required methods."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    return mock


def create_mock_container_runner():
    """Create a mock ContainerRunner that succeeds."""
    mock = MagicMock()
    mock.run = AsyncMock()
    return mock


def create_mock_github_client():
    """Create a mock GitHubClient."""
    mock = MagicMock()
    mock.close = AsyncMock()
    return mock


class TestCIFixAttemptStatusCommentsTS007:
    """Test TS-007: CI fix attempts post comments with correct counts."""

    @pytest.mark.asyncio
    async def test_first_attempt_posts_comment_with_1_of_max(self):
        """TS-007: Verify first CI fix attempt posts comment with '1/{max_attempts}' format."""
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

        # Verify status comment posted with correct format
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-300"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (1/3)."

        # Verify JiraClient closed
        assert mock_jira.close.call_count == 1

    @pytest.mark.asyncio
    async def test_second_attempt_posts_comment_with_2_of_max(self):
        """TS-007: Verify second CI fix attempt posts comment with '2/{max_attempts}' format."""
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

        # Verify status comment posted with correct format
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-301"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (2/3)."

    @pytest.mark.asyncio
    async def test_final_attempt_posts_comment_with_max_of_max(self):
        """TS-007: Verify final CI fix attempt posts comment with '{max_attempts}/{max_attempts}' format."""
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

        # Verify status comment posted with correct format
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-302"
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (3/3)."

    @pytest.mark.asyncio
    async def test_comment_posted_to_feature_ticket_not_task(self):
        """TS-007: Verify comment posted to feature ticket, not task tickets."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-303",
            current_repo="owner/test-repo",
            task_keys=["TASK-001", "TASK-002"],  # Multiple tasks
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
        state["max_attempts"] = 5

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

        # Verify comment posted to feature ticket (FEAT-303), not task tickets (TASK-001, TASK-002)
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][0] == "FEAT-303"
        assert "TASK-001" not in comment_call[0][0]
        assert "TASK-002" not in comment_call[0][0]


class TestCIFixAttemptCommentCounts:
    """Test that CI fix attempt comments include correct attempt counts."""

    @pytest.mark.asyncio
    async def test_multiple_attempts_show_incrementing_counts(self):
        """Verify multiple attempts show incrementing counts (1/3, 2/3, 3/3)."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        # Collect all comments posted
        comments = []
        
        def capture_comment(ticket_key, message):
            comments.append((ticket_key, message))
        
        mock_jira.add_comment.side_effect = capture_comment

        base_state = create_initial_feature_state(
            ticket_key="FEAT-304",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        base_state["ci_failed_checks"] = [
            {
                "pr_url": "https://github.com/owner/test-repo/pull/1",
                "name": "test-check",
                "conclusion": "failure",
                "output": {},
                "log_url": "https://github.com/owner/test-repo/runs/1",
            }
        ]
        base_state["workspace_path"] = "/tmp/test-workspace"
        base_state["max_attempts"] = 3

        # Simulate three attempts
        for attempt in [1, 2, 3]:
            state = {**base_state, "current_attempt": attempt}
            
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

        # Verify three comments posted with correct counts
        assert len(comments) == 3
        assert comments[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (1/3)."
        assert comments[1][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (2/3)."
        assert comments[2][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (3/3)."

    @pytest.mark.asyncio
    async def test_different_max_attempts_values(self):
        """Verify correct counts with different max_attempts values (e.g., 5)."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

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
        state["current_attempt"] = 2
        state["max_attempts"] = 5

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

        # Verify comment uses max_attempts=5
        assert mock_jira.add_comment.call_count == 1
        comment_call = mock_jira.add_comment.call_args
        assert comment_call[0][1] == "🔧 CI checks failed. Analyzing failure and attempting fix (2/5)."


class TestCIFixAttemptEdgeCases:
    """Test edge cases for CI fix attempt status comments."""

    @pytest.mark.asyncio
    async def test_missing_current_attempt_logs_error_skips_comment(self, caplog):
        """Verify missing current_attempt logs error and skips comment posting."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-306",
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
        # current_attempt is None (missing)
        state["current_attempt"] = None
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

        # Verify no comment posted
        assert mock_jira.add_comment.call_count == 0
        # Verify JiraClient still closed (even though comment wasn't posted)
        assert mock_jira.close.call_count == 1
        # Verify error logged
        assert any("CI fix attempt values unavailable" in record.message for record in caplog.records)
        assert any("current_attempt=None" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_max_attempts_logs_error_skips_comment(self, caplog):
        """Verify missing max_attempts logs error and skips comment posting."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-307",
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
        # max_attempts is None (missing)
        state["max_attempts"] = None

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

        # Verify no comment posted
        assert mock_jira.add_comment.call_count == 0
        # Verify error logged
        assert any("CI fix attempt values unavailable" in record.message for record in caplog.records)
        assert any("max_attempts=None" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_workflow_continues_when_both_values_missing(self, caplog):
        """Verify workflow continues when both current_attempt and max_attempts are missing."""
        mock_jira = create_mock_jira_client()
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-308",
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
        # Both values missing
        state["current_attempt"] = None
        state["max_attempts"] = None

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

        # Verify workflow continues (doesn't crash)
        assert result is not None
        # Verify no comment posted
        assert mock_jira.add_comment.call_count == 0
        # Verify error logged
        assert any("CI fix attempt values unavailable" in record.message for record in caplog.records)


class TestCIFixAttemptErrorHandling:
    """Test error handling for CI fix attempt status comments."""

    @pytest.mark.asyncio
    async def test_workflow_continues_when_comment_posting_fails(self, caplog):
        """Verify workflow continues when status comment posting fails."""
        mock_jira = create_mock_jira_client()
        # Simulate comment posting failure
        mock_jira.add_comment.side_effect = Exception("Jira API error")
        
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-309",
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

        # Verify workflow continues (doesn't raise exception)
        assert result is not None
        # Verify error logged by post_status_comment utility
        assert any("Failed to post status comment" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_jira_client_closed_even_on_comment_error(self):
        """Verify JiraClient closed even when comment posting fails."""
        mock_jira = create_mock_jira_client()
        # Simulate comment posting failure
        mock_jira.add_comment.side_effect = Exception("Jira API error")
        
        mock_runner = create_mock_container_runner()
        mock_github = create_mock_github_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-310",
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

        # Verify JiraClient closed despite error
        assert mock_jira.close.call_count == 1

    @pytest.mark.asyncio
    async def test_no_comment_posted_when_no_failed_checks(self):
        """Verify no comment posted when ci_failed_checks is empty."""
        mock_jira = create_mock_jira_client()

        state = create_initial_feature_state(
            ticket_key="FEAT-311",
            current_repo="owner/test-repo",
            task_keys=["TASK-001"],
        )
        # No failed checks
        state["ci_failed_checks"] = []
        state["current_attempt"] = 1
        state["max_attempts"] = 3

        result = await attempt_ci_fix(state)

        # Verify no comment posted (early return)
        assert mock_jira.add_comment.call_count == 0
        # Verify early return
        assert result["current_node"] == "ci_evaluator"
