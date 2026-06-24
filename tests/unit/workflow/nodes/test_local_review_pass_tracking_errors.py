"""Unit tests for defensive pass number tracking error handling in local_reviewer.py."""

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.workflow.nodes.local_reviewer import _validate_pass_number, local_review_changes


class TestValidatePassNumber:
    """Test the _validate_pass_number function."""

    def test_validate_pass_number_with_valid_positive_integer(self):
        """Verify valid positive integers pass validation."""
        assert _validate_pass_number(1) == 1
        assert _validate_pass_number(2) == 2
        assert _validate_pass_number(10) == 10
        assert _validate_pass_number(100) == 100

    def test_validate_pass_number_with_none(self):
        """Verify None returns None without logging warning."""
        assert _validate_pass_number(None) is None

    def test_validate_pass_number_with_negative_integer(self, caplog):
        """Verify negative integers are rejected and logged."""
        with caplog.at_level(logging.WARNING):
            result = _validate_pass_number(-1)
        assert result is None
        assert "Invalid pass_number value: -1" in caplog.text
        assert "expected positive integer >= 1" in caplog.text

    def test_validate_pass_number_with_zero(self, caplog):
        """Verify zero is rejected and logged."""
        with caplog.at_level(logging.WARNING):
            result = _validate_pass_number(0)
        assert result is None
        assert "Invalid pass_number value: 0" in caplog.text

    def test_validate_pass_number_with_string(self, caplog):
        """Verify string values are rejected and logged."""
        with caplog.at_level(logging.WARNING):
            result = _validate_pass_number("2")  # type: ignore
        assert result is None
        assert "Invalid pass_number type: str" in caplog.text
        assert "expected int" in caplog.text

    def test_validate_pass_number_with_float(self, caplog):
        """Verify float values are rejected and logged."""
        with caplog.at_level(logging.WARNING):
            result = _validate_pass_number(2.5)  # type: ignore
        assert result is None
        assert "Invalid pass_number type: float" in caplog.text

    def test_validate_pass_number_with_boolean(self, caplog):
        """Verify boolean values are rejected (even though bool is subclass of int)."""
        with caplog.at_level(logging.WARNING):
            result = _validate_pass_number(True)  # type: ignore
        assert result is None
        assert "Invalid pass_number type: bool" in caplog.text


class TestPassTrackingUnavailable:
    """Test behavior when pass_number is unavailable or None."""

    @pytest.mark.asyncio
    async def test_none_pass_number_posts_generic_comment(self, caplog):
        """Verify generic fallback comment posts when pass_number is None."""
        state = {
            "ticket_key": "TEST-123",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": None,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "No issues found"
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment") as mock_post, \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify WARNING logged about pass tracking failure
            assert "Pass number tracking unavailable or corrupted" in caplog.text
            assert "raw value: None" in caplog.text
            assert "using generic status comment" in caplog.text

            # Verify generic fallback comment was posted
            mock_post.assert_any_call(
                mock_jira,
                "TEST-123",
                "🔧 Local review found issues, applying fixes.",
            )

            # Verify workflow continued successfully
            assert result["current_node"] == "create_pr"

    @pytest.mark.asyncio
    async def test_workflow_continues_when_pass_number_unavailable(self):
        """Verify workflow execution continues when pass_number tracking fails."""
        state = {
            "ticket_key": "TEST-456",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": None,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "All good"
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify container was executed despite pass tracking failure
            assert mock_runner.run.called
            # Verify state transitioned to create_pr
            assert result["current_node"] == "create_pr"


class TestInvalidPassNumberValues:
    """Test behavior with invalid pass_number values (negative, non-integer, etc.)."""

    @pytest.mark.asyncio
    async def test_negative_pass_number_detected_and_logged(self, caplog):
        """Verify negative pass_number is detected, logged, and generic comment posted."""
        state = {
            "ticket_key": "TEST-789",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": -5,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment") as mock_post, \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify validation warning was logged
            assert "Invalid pass_number value: -5" in caplog.text
            # Verify fallback warning was logged
            assert "Pass number tracking unavailable or corrupted" in caplog.text
            assert "raw value: -5" in caplog.text

            # Verify generic comment posted
            mock_post.assert_any_call(
                mock_jira,
                "TEST-789",
                "🔧 Local review found issues, applying fixes.",
            )

            # Verify workflow continued
            assert result["current_node"] == "create_pr"

    @pytest.mark.asyncio
    async def test_non_integer_pass_number_detected_and_logged(self, caplog):
        """Verify non-integer pass_number (string) is detected and logged."""
        state = {
            "ticket_key": "TEST-999",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": "two",  # String instead of int
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment") as mock_post, \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify validation warning
            assert "Invalid pass_number type: str" in caplog.text
            assert "value: two" in caplog.text

            # Verify fallback comment posted
            mock_post.assert_any_call(
                mock_jira,
                "TEST-999",
                "🔧 Local review found issues, applying fixes.",
            )

            assert result["current_node"] == "create_pr"

    @pytest.mark.asyncio
    async def test_zero_pass_number_rejected_with_generic_comment(self, caplog):
        """Verify zero pass_number is rejected and workflow continues."""
        state = {
            "ticket_key": "TEST-000",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": 0,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment") as mock_post, \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify validation warning
            assert "Invalid pass_number value: 0" in caplog.text

            # Verify generic comment posted
            mock_post.assert_any_call(
                mock_jira,
                "TEST-000",
                "🔧 Local review found issues, applying fixes.",
            )

            assert result["current_node"] == "create_pr"


class TestNormalPassNumberLogging:
    """Test INFO-level logging for normal pass number increments."""

    @pytest.mark.asyncio
    async def test_pass_one_logs_info_message(self, caplog):
        """Verify INFO-level log for pass 1."""
        state = {
            "ticket_key": "TEST-111",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": 1,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"), \
             caplog.at_level(logging.INFO):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            await local_review_changes(state)

            # Verify INFO log for pass 1
            assert "Starting local review pass 1 for TEST-111" in caplog.text

    @pytest.mark.asyncio
    async def test_pass_two_logs_info_message(self, caplog):
        """Verify INFO-level log for pass 2."""
        state = {
            "ticket_key": "TEST-222",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 1,
            "local_review_pass_number": 2,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"), \
             caplog.at_level(logging.INFO):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            await local_review_changes(state)

            # Verify INFO log for pass 2
            assert "Starting local review pass 2 for TEST-222" in caplog.text

    @pytest.mark.asyncio
    async def test_pass_five_logs_info_message(self, caplog):
        """Verify INFO-level log for higher pass numbers."""
        state = {
            "ticket_key": "TEST-555",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 1,
            "local_review_pass_number": 5,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"), \
             caplog.at_level(logging.INFO):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            await local_review_changes(state)

            # Verify INFO log for pass 5
            assert "Starting local review pass 5 for TEST-555" in caplog.text


class TestPassTrackingFailureLogging:
    """Test WARNING-level logging for pass tracking failures with diagnostic info."""

    @pytest.mark.asyncio
    async def test_warning_log_includes_ticket_key(self, caplog):
        """Verify WARNING log includes ticket key for troubleshooting."""
        state = {
            "ticket_key": "DIAG-123",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": None,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"), \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            await local_review_changes(state)

            # Verify diagnostic information in WARNING log
            assert "DIAG-123" in caplog.text
            assert "Pass number tracking unavailable or corrupted" in caplog.text
            assert "raw value: None" in caplog.text

    @pytest.mark.asyncio
    async def test_warning_log_includes_raw_value_diagnostic(self, caplog):
        """Verify WARNING log includes raw value for debugging."""
        state = {
            "ticket_key": "DIAG-456",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": "invalid",
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"), \
             caplog.at_level(logging.WARNING):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            await local_review_changes(state)

            # Verify diagnostic information includes raw value
            assert "raw value: 'invalid'" in caplog.text


class TestPassNumberIncrement:
    """Test that pass_number increments safely after validation."""

    @pytest.mark.asyncio
    async def test_pass_number_increments_correctly_after_retry(self):
        """Verify pass_number increments properly in retry state update."""
        state = {
            "ticket_key": "TEST-INC",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": 2,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "unfixed breaking issues remain"
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify pass_number was incremented correctly
            assert result["local_review_pass_number"] == 3
            assert result["current_node"] == "local_review"

    @pytest.mark.asyncio
    async def test_pass_number_recovers_from_none_and_increments(self):
        """Verify pass_number recovers from None (defaults to 1) and can increment."""
        state = {
            "ticket_key": "TEST-RECOVER",
            "workspace_path": "/workspace/test",
            "local_review_attempts": 0,
            "local_review_pass_number": None,
            "context": {"branch_name": "test-branch", "guardrails": ""},
            "spec_content": "Test spec",
            "current_repo": "test/repo",
        }

        mock_jira = AsyncMock()
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "unfixed breaking issues remain"
        mock_result.stderr = ""
        mock_runner.run = AsyncMock(return_value=mock_result)

        with patch("forge.workflow.nodes.local_reviewer.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.local_reviewer.ContainerRunner", return_value=mock_runner), \
             patch("forge.workflow.nodes.local_reviewer.load_prompt", return_value="test prompt"), \
             patch("forge.workflow.nodes.local_reviewer.GitOperations") as mock_git_ops, \
             patch("forge.workflow.nodes.local_reviewer.post_status_comment"):

            mock_git_instance = MagicMock()
            mock_git_instance.has_uncommitted_changes.return_value = False
            mock_git_ops.return_value = mock_git_instance

            result = await local_review_changes(state)

            # Verify pass_number recovered to 1 and then incremented to 2
            assert result["local_review_pass_number"] == 2
            assert result["current_node"] == "local_review"
