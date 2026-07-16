"""Unit tests for implement_task node."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType

pytestmark = pytest.mark.usefixtures("mock_implementation_workspace_recovery")


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
    async def test_successful_implementation_is_pushed_before_checkpoint(self) -> None:
        """A different worker can recover the implementation commit from the fork."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state()
        mock_git = MagicMock()
        mock_jira = _make_mock_jira()
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                return_value=(state["workspace_path"], mock_git),
            ),
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(state)

        assert result["last_error"] is None
        mock_git.push_to_fork.assert_called_once()

    @pytest.mark.asyncio
    async def test_final_push_failure_is_recorded_for_retry(self) -> None:
        """The all-tasks-done path must not leak a push exception from the graph node."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(current_task_key=None)
        state["task_keys"] = []
        mock_git = MagicMock()
        mock_git.has_uncommitted_changes.return_value = False
        mock_git.push_to_fork.side_effect = RuntimeError("fork unavailable")

        with patch(
            "forge.workflow.nodes.implementation.prepare_workspace",
            return_value=(state["workspace_path"], mock_git),
        ):
            result = await implement_task(state)

        assert result["current_node"] == "implement_bug_fix"
        assert result["last_error"] == "fork unavailable"
        assert result["retry_count"] == 0
        assert result["persistence_retry_count"] == 3

    @pytest.mark.asyncio
    async def test_pending_push_retries_without_rerunning_container(self, tmp_path) -> None:
        """A surviving workspace resumes at persistence, not implementation."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(workspace_path=str(tmp_path))
        state["implementation_push_pending"] = True
        state["implementation_push_pending_task"] = "TASK-456"
        mock_git = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch("forge.workflow.nodes.implementation.ContainerRunner") as runner,
        ):
            result = await implement_task(state)

        runner.assert_not_called()
        mock_git.push_to_fork.assert_called_once()
        assert result["implemented_tasks"] == ["TASK-456"]
        assert result["implementation_push_pending"] is False

    @pytest.mark.asyncio
    async def test_recreated_workspace_does_not_mark_pending_task_complete(self, tmp_path) -> None:
        """A replacement clone cannot stand in for the workspace holding the commit."""
        from forge.workflow.nodes.implementation import implement_task

        old_workspace = tmp_path / "old"
        old_workspace.mkdir()
        new_workspace = tmp_path / "new"
        new_workspace.mkdir()
        state = _make_state(workspace_path=str(old_workspace))
        state["implementation_push_pending"] = True
        state["implementation_push_pending_task"] = "TASK-456"
        mock_git = MagicMock()
        mock_jira = _make_mock_jira()
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                return_value=(str(new_workspace), mock_git),
            ),
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(state)

        runner.run.assert_awaited_once()
        assert result["implementation_push_pending"] is False

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
    """Consolidated tests for structured logging in implement_task (TS-001 through TS-007)."""

    @pytest.mark.asyncio
    async def test_logs_implementation_started_with_structured_fields(self, caplog):
        """TS-001: Verify start log has correct extra fields."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix authentication bug")
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-123",
                    current_task_key="TASK-456",
                    tasks_by_repo={"acme/backend": ["TASK-456"]},
                )
            )

        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1

        record = started_records[0]
        assert record.levelname == "INFO"
        assert record.event == "implementation_started"
        assert record.task_name == "Fix authentication bug"
        assert record.feature_id == "FEAT-123"
        assert record.task_id == "TASK-456"

    @pytest.mark.asyncio
    async def test_logs_implementation_completed_on_success(self, caplog):
        """TS-002: Verify success end log has success=True."""
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-200",
                    current_task_key="TASK-300",
                    tasks_by_repo={"acme/backend": ["TASK-300"]},
                )
            )

        completed_records = [
            r for r in caplog.records if "Implementation completed for task" in r.message
        ]
        assert len(completed_records) == 1

        record = completed_records[0]
        assert record.levelname == "INFO"
        assert record.event == "implementation_completed"
        assert record.task_name == "Add caching layer"
        assert record.feature_id == "FEAT-200"
        assert record.task_id == "TASK-300"
        assert record.success is True

    @pytest.mark.asyncio
    async def test_logs_implementation_ended_on_failure(self, caplog):
        """TS-003: Verify failure end log has success=False."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Refactor database layer")
        runner = MagicMock()
        container_result = MagicMock()
        container_result.success = False
        container_result.error_message = "container crashed"
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-400",
                    current_task_key="TASK-500",
                    tasks_by_repo={"acme/backend": ["TASK-500"]},
                )
            )

        ended_records = [r for r in caplog.records if "Implementation ended for task" in r.message]
        assert len(ended_records) == 1

        record = ended_records[0]
        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.task_name == "Refactor database layer"
        assert record.feature_id == "FEAT-400"
        assert record.task_id == "TASK-500"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_implementation_ended_on_exception(self, caplog):
        """TS-004: Verify exception path emits end log."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Update API endpoints")
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=RuntimeError("unexpected error"))

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
            result = await implement_task(
                _make_state(
                    ticket_key="FEAT-600",
                    current_task_key="TASK-700",
                    tasks_by_repo={"acme/backend": ["TASK-700"]},
                )
            )

        # Verify failure was captured in state
        assert "unexpected error" in result["last_error"]

        ended_records = [r for r in caplog.records if "Implementation ended for task" in r.message]
        assert len(ended_records) == 1

        record = ended_records[0]
        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.task_name == "Update API endpoints"
        assert record.feature_id == "FEAT-600"
        assert record.task_id == "TASK-700"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_unknown_task_name_when_jira_fails_early(self, caplog):
        """TS-005: Verify placeholder handling when Jira fetch fails early."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = AsyncMock()
        mock_jira.get_issue = AsyncMock(side_effect=Exception("Jira unavailable"))
        mock_jira.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            result = await implement_task(
                _make_state(
                    ticket_key="FEAT-800",
                    current_task_key="TASK-900",
                    tasks_by_repo={"acme/backend": ["TASK-900"]},
                )
            )

        # Verify failure was recorded
        assert "Jira unavailable" in result["last_error"]

        ended_records = [r for r in caplog.records if "Implementation ended for task" in r.message]
        assert len(ended_records) == 1

        record = ended_records[0]
        assert record.levelname == "INFO"
        assert record.event == "implementation_ended"
        assert record.task_name == "unknown"
        assert record.feature_id == "FEAT-800"
        assert record.task_id == "TASK-900"
        assert record.success is False

    @pytest.mark.asyncio
    async def test_logs_empty_task_summary_as_empty_string(self, caplog):
        """TS-006: Verify empty summary handling - logs empty string."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="", description="Some task description")
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-1000",
                    current_task_key="TASK-1100",
                    tasks_by_repo={"acme/backend": ["TASK-1100"]},
                )
            )

        # Check started log
        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1
        assert started_records[0].task_name == ""

        # Check completed log
        completed_records = [
            r for r in caplog.records if "Implementation completed for task" in r.message
        ]
        assert len(completed_records) == 1
        assert completed_records[0].task_name == ""

    @pytest.mark.asyncio
    async def test_logs_special_characters_in_task_summary(self, caplog):
        """TS-007: Verify special chars not escaped in task_name field."""
        from forge.workflow.nodes.implementation import implement_task

        special_summary = "Fix: <script>alert('XSS')</script> & unicode: 日本語 © ® ™"
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-1200",
                    current_task_key="TASK-1300",
                    tasks_by_repo={"acme/backend": ["TASK-1300"]},
                )
            )

        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1

        record = started_records[0]
        # Special characters should be preserved as-is, not escaped
        assert record.task_name == special_summary
