"""Unit tests for implement_task node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType


def _make_state(
    ticket_key="BUG-123",
    ticket_type=TicketType.BUG,
    current_task_key="TASK-456",
    workspace_path="/tmp/ws",
    current_repo="acme/backend",
    tasks_by_repo=None,
    implemented_tasks=None,
):
    return {
        "ticket_key": ticket_key,
        "ticket_type": ticket_type,
        "current_node": "implement_task",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "workspace_path": workspace_path,
        "current_task_key": current_task_key,
        "current_repo": current_repo,
        "task_keys": [current_task_key] if current_task_key else [],
        "tasks_by_repo": tasks_by_repo or {current_repo: [current_task_key]},
        "implemented_tasks": implemented_tasks or [],
        "context": {"branch_name": "forge/BUG-123", "guardrails": ""},
        "fork_owner": "forge-bot",
        "fork_repo": "backend",
    }


def _make_mock_jira(summary="Fix null pointer in AuthService", description="Details"):
    jira = AsyncMock()
    issue = MagicMock()
    issue.summary = summary
    issue.description = description
    jira.get_issue = AsyncMock(return_value=issue)
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()
    return jira


def _make_successful_runner():
    runner = MagicMock()
    result = MagicMock()
    result.success = True
    result.error_message = None
    runner.run = AsyncMock(return_value=result)
    return runner


class TestImplementTaskStartedComment:
    @pytest.mark.asyncio
    async def test_posts_comment_on_task_ticket_before_container(self):
        """A comment is posted on the task ticket (not parent) when implementation starts."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix null pointer in AuthService")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            await implement_task(_make_state())

        mock_jira.add_comment.assert_any_call(
            "TASK-456",
            "🔨 Forge started implementing [TASK-456]: Fix null pointer in AuthService",
        )

    @pytest.mark.asyncio
    async def test_comment_mentions_correct_task_key(self):
        """The comment body contains the child task key and summary."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add retry logic")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-99",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        call_args = mock_jira.add_comment.call_args_list[0]
        assert call_args[0][0] == "TASK-100"
        assert "TASK-100" in call_args[0][1]
        assert "Add retry logic" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_comment_failure_does_not_block_implementation(self):
        """If posting the comment raises, implementation still proceeds."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira()
        mock_jira.add_comment = AsyncMock(side_effect=Exception("Jira unreachable"))
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(_make_state())

        # Implementation succeeded despite comment failure
        assert result["last_error"] is None
        assert "TASK-456" in result["implemented_tasks"]

    @pytest.mark.asyncio
    async def test_passes_trace_context_to_container_runner(self):
        """Container traces receive workflow fields for configured labels."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix null pointer in AuthService")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-99",
                    ticket_type=TicketType.FEATURE,
                    current_repo="acme/backend",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        trace_context = runner.run.call_args.kwargs["trace_context"]
        assert trace_context == {
            "ticket_key": "FEAT-99",
            "ticket_type": TicketType.FEATURE,
            "current_node": "implement_task",
            "current_repo": "acme/backend",
            "repo": "acme/backend",
            "current_pr_number": None,
            "pr_number": None,
            "retry_count": 0,
        }


class TestImplementationNodeRouting:
    @pytest.mark.asyncio
    async def test_feature_missing_workspace_uses_feature_implementation_node(self):
        """Feature implementation failures must resume at implement_task."""
        from forge.workflow.nodes.implementation import implement_task

        result = await implement_task(
            _make_state(
                ticket_key="FEAT-123",
                ticket_type=TicketType.FEATURE,
                workspace_path=None,
            )
        )

        assert result["current_node"] == "implement_task"
        assert result["last_error"] == "Workspace not set up"

    @pytest.mark.asyncio
    async def test_bug_missing_workspace_keeps_bug_implementation_node(self):
        """Bug implementation failures must still resume at implement_bug_fix."""
        from forge.workflow.nodes.implementation import implement_task

        result = await implement_task(_make_state(workspace_path=None))

        assert result["current_node"] == "implement_bug_fix"
        assert result["last_error"] == "Workspace not set up"

    @pytest.mark.asyncio
    async def test_feature_container_failure_uses_feature_implementation_node(self):
        """Feature container failures must not checkpoint bug workflow node names."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira()
        runner = MagicMock()
        container_result = MagicMock()
        container_result.success = False
        container_result.error_message = "container failed"
        runner.run = AsyncMock(return_value=container_result)

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(
                _make_state(ticket_key="FEAT-123", ticket_type=TicketType.FEATURE)
            )

        assert result["current_node"] == "implement_task"
        assert result["last_error"] == "container failed"
        assert result["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_bug_container_failure_keeps_bug_implementation_node(self):
        """Bug container failures keep the bug graph retry node."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira()
        runner = MagicMock()
        container_result = MagicMock()
        container_result.success = False
        container_result.error_message = "container failed"
        runner.run = AsyncMock(return_value=container_result)

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(_make_state())

        assert result["current_node"] == "implement_bug_fix"
        assert result["last_error"] == "container failed"
        assert result["retry_count"] == 1


class TestImplementTaskStructuredLogging:
    """Tests for structured logging in implement_task (TS-001 through TS-007)."""

    @pytest.mark.asyncio
    async def test_logs_implementation_started_with_structured_fields(self, caplog):
        """TS-001: Verify start log has correct extra fields."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add caching layer")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-100", current_task_key="TASK-200"))

        # Find the start log record
        start_records = [r for r in caplog.records if "implementation_started" in str(r.__dict__)]
        assert len(start_records) == 1
        record = start_records[0]

        assert record.levelname == "INFO"
        assert record.event == "implementation_started"
        assert record.task_name == "Add caching layer"
        assert record.feature_id == "FEAT-100"
        assert record.task_id == "TASK-200"

    @pytest.mark.asyncio
    async def test_logs_implementation_completed_on_success(self, caplog):
        """TS-002: Verify success end log has success=True."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Refactor DB queries")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-101", current_task_key="TASK-201"))

        # Find the completion log record
        end_records = [r for r in caplog.records if "implementation_completed" in str(r.__dict__)]
        assert len(end_records) == 1
        record = end_records[0]

        assert record.levelname == "INFO"
        assert record.event == "implementation_completed"
        assert record.task_name == "Refactor DB queries"
        assert record.feature_id == "FEAT-101"
        assert record.task_id == "TASK-201"
        assert record.success is True

    @pytest.mark.asyncio
    async def test_logs_implementation_ended_on_failure(self, caplog):
        """TS-003: Verify failure end log has success=False."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix memory leak")
        runner = MagicMock()
        container_result = MagicMock()
        container_result.success = False
        container_result.error_message = "container failed"
        runner.run = AsyncMock(return_value=container_result)

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-102", current_task_key="TASK-202"))

        # Find the failure log record
        end_records = [r for r in caplog.records if "implementation_ended" in str(r.__dict__)]
        assert len(end_records) == 1
        record = end_records[0]

        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.task_name == "Fix memory leak"
        assert record.feature_id == "FEAT-102"
        assert record.task_id == "TASK-202"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_implementation_ended_on_exception(self, caplog):
        """TS-004: Verify exception path emits end log."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add retry logic")
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=RuntimeError("Connection timeout"))

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-103", current_task_key="TASK-203"))

        # Find the failure log record
        end_records = [r for r in caplog.records if "implementation_ended" in str(r.__dict__)]
        assert len(end_records) == 1
        record = end_records[0]

        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.feature_id == "FEAT-103"
        assert record.task_id == "TASK-203"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_unknown_task_name_when_jira_fails_early(self, caplog):
        """TS-005: Verify placeholder handling when Jira fetch fails."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = AsyncMock()
        mock_jira.get_issue = AsyncMock(side_effect=Exception("Jira API timeout"))
        mock_jira.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-104", current_task_key="TASK-204"))

        # Find the failure log record
        end_records = [r for r in caplog.records if "implementation_ended" in str(r.__dict__)]
        assert len(end_records) == 1
        record = end_records[0]

        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.task_name == "unknown"
        assert record.feature_id == "FEAT-104"
        assert record.task_id == "TASK-204"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_empty_task_summary_as_empty_string(self, caplog):
        """TS-006: Verify empty summary handling."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="")
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-105", current_task_key="TASK-205"))

        # Find the start log record
        start_records = [r for r in caplog.records if "implementation_started" in str(r.__dict__)]
        assert len(start_records) == 1
        record = start_records[0]

        assert record.levelname == "INFO"
        assert record.task_name == ""

    @pytest.mark.asyncio
    async def test_logs_special_characters_in_task_summary(self, caplog):
        """TS-007: Verify special chars not escaped."""
        from forge.workflow.nodes.implementation import implement_task

        special_summary = 'Fix <script>alert("xss")</script> & handle "quotes"'
        mock_jira = _make_mock_jira(summary=special_summary)
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(_make_state(ticket_key="FEAT-106", current_task_key="TASK-206"))

        # Find the start log record
        start_records = [r for r in caplog.records if "implementation_started" in str(r.__dict__)]
        assert len(start_records) == 1
        record = start_records[0]

        assert record.levelname == "INFO"
        assert record.task_name == special_summary
