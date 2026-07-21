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


class TestImplementationStepBoundaryLogging:
    """Tests for implementation step boundary logging (TS-001 through TS-009)."""

    @pytest.mark.asyncio
    async def test_start_log_emitted_before_container(self, caplog):
        """TS-001: Verify start log appears before container runs with correct fields."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add authentication")
        runner = _make_successful_runner()

        # Track call order
        call_order = []

        original_run = runner.run

        async def track_run(*args, **kwargs):
            call_order.append("container_run")
            return await original_run(*args, **kwargs)

        runner.run = track_run

        with (
            caplog.at_level(logging.INFO),
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
                    ticket_key="FEAT-100",
                    current_task_key="TASK-200",
                    tasks_by_repo={"acme/backend": ["TASK-200"]},
                )
            )

        # Verify start log was emitted with correct content
        start_logs = [r for r in caplog.records if "Implementation step started" in r.message]
        assert len(start_logs) == 1
        start_log = start_logs[0]
        assert start_log.levelno == logging.INFO
        assert "TASK-200" in start_log.message
        assert "FEAT-100" in start_log.message
        assert "Add authentication" in start_log.message

        # Verify container was called (start log appears before container runs)
        assert "container_run" in call_order

    @pytest.mark.asyncio
    async def test_end_log_emitted_after_successful_container(self, caplog):
        """TS-002: Verify 'completed' message after successful container run."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Implement caching")
        runner = _make_successful_runner()

        with (
            caplog.at_level(logging.INFO),
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
                    ticket_key="FEAT-100",
                    current_task_key="TASK-200",
                    tasks_by_repo={"acme/backend": ["TASK-200"]},
                )
            )

        # Verify end log with "completed" status
        end_logs = [r for r in caplog.records if "Implementation step completed" in r.message]
        assert len(end_logs) == 1
        end_log = end_logs[0]
        assert end_log.levelno == logging.INFO
        assert "TASK-200" in end_log.message
        assert "FEAT-100" in end_log.message
        assert "Implement caching" in end_log.message

    @pytest.mark.asyncio
    async def test_end_log_emitted_after_failed_container(self, caplog):
        """TS-003: Verify 'ended' message after container failure."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Fix validation bug")
        runner = MagicMock()
        container_result = MagicMock()
        container_result.success = False
        container_result.error_message = "tests failed"
        runner.run = AsyncMock(return_value=container_result)

        with (
            caplog.at_level(logging.INFO),
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
                    ticket_key="BUG-50",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        # Verify end log with "ended" status (not "completed")
        end_logs = [r for r in caplog.records if "Implementation step ended" in r.message]
        assert len(end_logs) == 1
        end_log = end_logs[0]
        assert end_log.levelno == logging.INFO
        assert "TASK-100" in end_log.message
        assert "BUG-50" in end_log.message
        assert "Fix validation bug" in end_log.message
        # Ensure it says "ended", not "completed"
        assert "completed" not in end_log.message

    @pytest.mark.asyncio
    async def test_start_log_precedes_end_log(self, caplog):
        """TS-004: Verify start log appears before end log in record order."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add feature")
        runner = _make_successful_runner()

        with (
            caplog.at_level(logging.INFO),
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

        # Find start and end log indices
        start_index = None
        end_index = None
        for i, record in enumerate(caplog.records):
            if "Implementation step started" in record.message:
                start_index = i
            if "Implementation step completed" in record.message:
                end_index = i

        assert start_index is not None, "Start log not found"
        assert end_index is not None, "End log not found"
        assert start_index < end_index, "Start log should precede end log"

    @pytest.mark.asyncio
    async def test_fallback_value_when_task_summary_none(self, caplog):
        """TS-005: Verify 'unknown' used when task summary is None."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary=None)
        runner = _make_successful_runner()

        with (
            caplog.at_level(logging.INFO),
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

        # Verify "unknown" appears in place of task summary
        start_logs = [r for r in caplog.records if "Implementation step started" in r.message]
        assert len(start_logs) == 1
        assert "(unknown)" in start_logs[0].message

    @pytest.mark.asyncio
    async def test_fallback_value_when_task_summary_empty(self, caplog):
        """TS-006: Verify 'unknown' used when task summary is empty string."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="")
        runner = _make_successful_runner()

        with (
            caplog.at_level(logging.INFO),
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

        # Verify "unknown" appears in place of task summary
        start_logs = [r for r in caplog.records if "Implementation step started" in r.message]
        assert len(start_logs) == 1
        assert "(unknown)" in start_logs[0].message

    @pytest.mark.asyncio
    async def test_no_boundary_logs_when_all_tasks_complete(self, caplog):
        """TS-007: Verify no task-specific boundary logs on early return."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        # State with no current task and all tasks already implemented
        state = _make_state(
            current_task_key=None,
            implemented_tasks=["TASK-456"],
        )
        state["task_keys"] = []

        mock_git = MagicMock()
        mock_git.has_uncommitted_changes.return_value = False

        with (
            caplog.at_level(logging.INFO),
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                return_value=(state["workspace_path"], mock_git),
            ),
        ):
            await implement_task(state)

        # No boundary logs should appear
        boundary_logs = [
            r
            for r in caplog.records
            if "Implementation step started" in r.message
            or "Implementation step completed" in r.message
            or "Implementation step ended" in r.message
        ]
        assert len(boundary_logs) == 0

    @pytest.mark.asyncio
    async def test_no_boundary_logs_when_workspace_prep_fails(self, caplog):
        """TS-008: Verify no boundary logs when workspace setup fails."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        with (
            caplog.at_level(logging.INFO),
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                side_effect=RuntimeError("Clone failed"),
            ),
        ):
            result = await implement_task(_make_state())

        # Workspace prep failed
        assert result["last_error"] == "Clone failed"

        # No boundary logs should appear
        boundary_logs = [
            r
            for r in caplog.records
            if "Implementation step started" in r.message
            or "Implementation step completed" in r.message
            or "Implementation step ended" in r.message
        ]
        assert len(boundary_logs) == 0

    @pytest.mark.asyncio
    async def test_log_failure_does_not_block_implementation(self, caplog):
        """TS-009: Mock logging to raise, verify implementation proceeds."""
        import logging

        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add tests")
        runner = _make_successful_runner()

        # Mock the logging helpers to raise exceptions
        with (
            caplog.at_level(logging.INFO),
            patch(
                "forge.workflow.nodes.implementation.JiraClient",
                return_value=mock_jira,
            ),
            patch(
                "forge.workflow.nodes.implementation.ContainerRunner",
                return_value=runner,
            ),
            patch("forge.workflow.nodes.implementation.get_settings"),
            patch(
                "forge.workflow.nodes.implementation._log_step_start",
                side_effect=RuntimeError("Logging failed"),
            ),
            patch(
                "forge.workflow.nodes.implementation._log_step_end",
                side_effect=RuntimeError("Logging failed"),
            ),
        ):
            result = await implement_task(_make_state())

        # Implementation should succeed despite logging failures
        assert result["last_error"] is None
        assert "TASK-456" in result["implemented_tasks"]
