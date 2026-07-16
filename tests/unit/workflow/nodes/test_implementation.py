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


class TestImplementationStartedStructuredLog:
    """Tests for the structured log emitted when implementation starts."""

    @pytest.mark.asyncio
    async def test_implementation_started_log_emitted_with_extra_fields(self, caplog):
        """Structured log is emitted with correct extra fields."""
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
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-99",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        # Find the implementation_started log record
        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1, "Expected exactly one implementation_started log"

        record = started_records[0]
        assert record.levelname == "INFO"
        assert "TASK-100" in record.message
        assert record.event == "implementation_started"
        assert record.task_name == "Add retry logic"
        assert record.feature_id == "FEAT-99"
        assert record.task_id == "TASK-100"

    @pytest.mark.asyncio
    async def test_implementation_started_log_empty_summary(self, caplog):
        """Empty task summary results in task_name="" in extra dict."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="", description="Some description")
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
                    ticket_key="BUG-50",
                    current_task_key="TASK-200",
                    tasks_by_repo={"acme/backend": ["TASK-200"]},
                )
            )

        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1

        record = started_records[0]
        assert record.task_name == ""

    @pytest.mark.asyncio
    async def test_implementation_started_log_special_characters_preserved(self, caplog):
        """Special characters in task summary are logged as-is."""
        from forge.workflow.nodes.implementation import implement_task

        special_summary = "Fix: <script>alert('XSS')</script> & unicode: 日本語"
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
                    ticket_key="FEAT-1",
                    current_task_key="TASK-1",
                    tasks_by_repo={"acme/backend": ["TASK-1"]},
                )
            )

        started_records = [
            r for r in caplog.records if "Implementation started for task" in r.message
        ]
        assert len(started_records) == 1

        record = started_records[0]
        assert record.task_name == special_summary


class TestImplementationCompletedStructuredLog:
    """Tests for the structured log emitted when implementation completes successfully."""

    @pytest.mark.asyncio
    async def test_implementation_completed_log_emitted_with_extra_fields(self, caplog):
        """Structured log is emitted with correct extra fields on success."""
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
            caplog.at_level("INFO", logger="forge.workflow.nodes.implementation"),
        ):
            await implement_task(
                _make_state(
                    ticket_key="FEAT-99",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        # Find the implementation_completed log record
        completed_records = [
            r for r in caplog.records if "Implementation completed for task" in r.message
        ]
        assert len(completed_records) == 1, "Expected exactly one implementation_completed log"

        record = completed_records[0]
        assert record.levelname == "INFO"
        assert "TASK-100" in record.message
        assert record.event == "implementation_completed"
        assert record.task_name == "Add retry logic"
        assert record.feature_id == "FEAT-99"
        assert record.task_id == "TASK-100"
        assert record.success is True

    @pytest.mark.asyncio
    async def test_implementation_completed_log_empty_summary(self, caplog):
        """Empty task summary results in task_name="" in extra dict."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="", description="Some description")
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
                    ticket_key="BUG-50",
                    current_task_key="TASK-200",
                    tasks_by_repo={"acme/backend": ["TASK-200"]},
                )
            )

        completed_records = [
            r for r in caplog.records if "Implementation completed for task" in r.message
        ]
        assert len(completed_records) == 1

        record = completed_records[0]
        assert record.task_name == ""
        assert record.success is True

    @pytest.mark.asyncio
    async def test_implementation_completed_log_not_emitted_on_failure(self, caplog):
        """The implementation_completed log is not emitted when container fails."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira(summary="Add retry logic")
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
            await implement_task(
                _make_state(
                    ticket_key="FEAT-99",
                    current_task_key="TASK-100",
                    tasks_by_repo={"acme/backend": ["TASK-100"]},
                )
            )

        # The implementation_completed log should NOT be emitted
        completed_records = [
            r for r in caplog.records if "Implementation completed for task" in r.message
        ]
        assert len(completed_records) == 0, (
            "implementation_completed log should not be emitted on failure"
        )
