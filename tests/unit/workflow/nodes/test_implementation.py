"""Unit tests for implement_task node."""

import logging
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
            patch("forge.workflow.nodes.implementation.notify_error", new_callable=AsyncMock),
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
            patch("forge.workflow.nodes.implementation.notify_error", new_callable=AsyncMock),
        ):
            result = await implement_task(_make_state())

        assert result["current_node"] == "implement_bug_fix"
        assert result["last_error"] == "container failed"
        assert result["retry_count"] == 1


class TestImplementationStepLogging:
    """Test lifecycle logging for implementation step."""

    @pytest.mark.asyncio
    async def test_start_log_emitted_with_all_fields(self, caplog):
        """Verify start log contains task name, feature_id, and task_id."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add authentication middleware")
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
            caplog.at_level(logging.INFO),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-200",
                    current_task_key="TASK-201",
                    tasks_by_repo={"acme/backend": ["TASK-201"]},
                )
            )

        # Verify start log contains all required fields
        assert "Implementation step started" in caplog.text
        assert "Add authentication middleware" in caplog.text  # task name
        assert "feature_id: FEAT-200" in caplog.text
        assert "task_id: TASK-201" in caplog.text

    @pytest.mark.asyncio
    async def test_start_log_level_is_info(self, caplog):
        """Verify start log is emitted at INFO level."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix login timeout")
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
            caplog.at_level(logging.INFO),
        ):
            await implement_task(
                _make_state(
                    ticket_key="BUG-300",
                    current_task_key="TASK-301",
                    tasks_by_repo={"acme/backend": ["TASK-301"]},
                )
            )

        # Find the start log record and verify its level
        start_log_records = [
            r for r in caplog.records if "Implementation step started" in r.message
        ]
        assert len(start_log_records) == 1
        assert start_log_records[0].levelno == logging.INFO

    @pytest.mark.asyncio
    async def test_start_log_contains_iso8601_timestamp(self, caplog):
        """Verify start log contains ISO 8601 formatted timestamp."""
        import re

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Implement user search")
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
            caplog.at_level(logging.INFO),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-400",
                    current_task_key="TASK-401",
                    tasks_by_repo={"acme/backend": ["TASK-401"]},
                )
            )

        # Find start log message and verify ISO 8601 timestamp format
        # ISO 8601 format: YYYY-MM-DDTHH:MM:SS.ffffff+HH:MM or ending with Z
        iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
        start_log_records = [
            r for r in caplog.records if "Implementation step started" in r.message
        ]
        assert len(start_log_records) == 1

        # Verify timestamp field exists and has ISO 8601 format
        assert "timestamp:" in start_log_records[0].message
        assert re.search(iso8601_pattern, start_log_records[0].message)

    @pytest.mark.asyncio
    async def test_end_log_emitted_on_success(self, caplog):
        """Verify end log emitted on successful implementation with all required fields."""
        import re

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Implement user profile API")
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
            caplog.at_level(logging.INFO),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-500",
                    current_task_key="TASK-501",
                    tasks_by_repo={"acme/backend": ["TASK-501"]},
                )
            )

        # Verify end log message is emitted
        end_log_records = [
            r for r in caplog.records if "Implementation step completed" in r.message
        ]
        assert len(end_log_records) == 1

        # Verify end log level is INFO
        assert end_log_records[0].levelno == logging.INFO

        # Verify end log contains all required fields
        end_message = end_log_records[0].message
        assert "task: Implement user profile API" in end_message  # task_name
        assert "feature_id: FEAT-500" in end_message
        assert "task_id: TASK-501" in end_message

        # Verify timestamp field exists with ISO 8601 format
        assert "timestamp:" in end_message
        iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
        assert re.search(iso8601_pattern, end_message)

        # Verify both start and end logs are emitted
        start_log_records = [
            r for r in caplog.records if "Implementation step started" in r.message
        ]
        assert len(start_log_records) == 1
        assert len(end_log_records) == 1  # Already verified above

    @pytest.mark.asyncio
    async def test_end_log_emitted_on_container_failure(self, caplog):
        """Verify end log is emitted even when container returns failure result."""
        import re

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Failing implementation task")
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
            patch("forge.workflow.nodes.implementation.notify_error", new_callable=AsyncMock),
            caplog.at_level(logging.INFO),
        ):
            result = await implement_task(
                _make_state(
                    ticket_key="FEAT-600",
                    ticket_type=TicketType.FEATURE,
                    current_task_key="TASK-601",
                    tasks_by_repo={"acme/backend": ["TASK-601"]},
                )
            )

        # Verify container did fail
        assert result["last_error"] == "container failed"
        assert result["retry_count"] == 1

        # Verify end log is still emitted despite failure
        end_log_records = [
            r for r in caplog.records if "Implementation step completed" in r.message
        ]
        assert len(end_log_records) == 1

        # Verify end log level is INFO
        assert end_log_records[0].levelno == logging.INFO

        # Verify end log contains all required fields
        end_message = end_log_records[0].message
        assert "task: Failing implementation task" in end_message
        assert "feature_id: FEAT-600" in end_message
        assert "task_id: TASK-601" in end_message

        # Verify timestamp field exists with ISO 8601 format
        assert "timestamp:" in end_message
        iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
        assert re.search(iso8601_pattern, end_message)

    @pytest.mark.asyncio
    async def test_end_log_emitted_on_exception(self, caplog):
        """Verify end log is emitted even when container raises an exception."""
        import re

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Exception causing task")
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=RuntimeError("Container crashed unexpectedly"))

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
            patch("forge.workflow.nodes.implementation.notify_error", new_callable=AsyncMock),
            caplog.at_level(logging.INFO),
        ):
            result = await implement_task(
                _make_state(
                    ticket_key="BUG-700",
                    current_task_key="TASK-701",
                    tasks_by_repo={"acme/backend": ["TASK-701"]},
                )
            )

        # Verify exception was caught and error state returned
        assert result["last_error"] == "Container crashed unexpectedly"
        assert result["retry_count"] == 1

        # Verify end log is emitted via try/finally (logged before error state returned)
        end_log_records = [
            r for r in caplog.records if "Implementation step completed" in r.message
        ]
        assert len(end_log_records) == 1

        # Verify end log level is INFO
        assert end_log_records[0].levelno == logging.INFO

        # Verify end log contains all required fields
        end_message = end_log_records[0].message
        assert "task: Exception causing task" in end_message
        assert "feature_id: BUG-700" in end_message
        assert "task_id: TASK-701" in end_message

        # Verify timestamp field exists with ISO 8601 format
        assert "timestamp:" in end_message
        iso8601_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"
        assert re.search(iso8601_pattern, end_message)

    @pytest.mark.asyncio
    async def test_log_uses_unknown_when_task_name_unavailable(self, caplog):
        """Verify 'unknown' is used in logs when task_issue.summary is None or empty."""
        from forge.workflow.nodes.implementation import implement_task

        # Test case 1: summary is None
        mock_jira_none = _make_mock_jira(summary=None)
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira_none,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level(logging.INFO),
        ):
            caplog.clear()
            result = await implement_task(
                _make_state(
                    ticket_key="FEAT-800",
                    current_task_key="TASK-801",
                    tasks_by_repo={"acme/backend": ["TASK-801"]},
                )
            )

        # Verify implementation did not fail
        assert result["last_error"] is None
        assert "TASK-801" in result["implemented_tasks"]

        # Verify start log uses "unknown" for task name when summary is None
        start_log_records = [
            r for r in caplog.records if "Implementation step started" in r.message
        ]
        assert len(start_log_records) == 1
        assert "task: unknown" in start_log_records[0].message

        # Verify end log uses "unknown" for task name when summary is None
        end_log_records = [
            r for r in caplog.records if "Implementation step completed" in r.message
        ]
        assert len(end_log_records) == 1
        assert "task: unknown" in end_log_records[0].message

        # Test case 2: summary is empty string
        mock_jira_empty = _make_mock_jira(summary="")
        runner2 = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira_empty,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner2,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level(logging.INFO),
        ):
            caplog.clear()
            result2 = await implement_task(
                _make_state(
                    ticket_key="FEAT-900",
                    current_task_key="TASK-901",
                    tasks_by_repo={"acme/backend": ["TASK-901"]},
                )
            )

        # Verify implementation did not fail
        assert result2["last_error"] is None
        assert "TASK-901" in result2["implemented_tasks"]

        # Verify start log uses "unknown" for task name when summary is empty
        start_log_records_empty = [
            r for r in caplog.records if "Implementation step started" in r.message
        ]
        assert len(start_log_records_empty) == 1
        assert "task: unknown" in start_log_records_empty[0].message

        # Verify end log uses "unknown" for task name when summary is empty
        end_log_records_empty = [
            r for r in caplog.records if "Implementation step completed" in r.message
        ]
        assert len(end_log_records_empty) == 1
        assert "task: unknown" in end_log_records_empty[0].message
