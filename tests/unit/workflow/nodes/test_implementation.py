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
    retry_count=0,
    handoffs=None,
):
    return {
        "ticket_key": ticket_key,
        "ticket_type": ticket_type,
        "current_node": "implement_task",
        "is_paused": False,
        "retry_count": retry_count,
        "last_error": None,
        "workspace_path": workspace_path,
        "current_task_key": current_task_key,
        "current_repo": current_repo,
        "task_keys": [current_task_key] if current_task_key else [],
        "tasks_by_repo": tasks_by_repo or {current_repo: [current_task_key]},
        "implemented_tasks": implemented_tasks or [],
        "handoffs": handoffs or {},
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
    async def test_prepare_workspace_failure_increments_retry_count(self):
        """Workspace setup failures must increment retry_count to avoid unbounded loops."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(workspace_path=None, retry_count=1)
        result = await implement_task(state)

        assert result["retry_count"] == 2
        assert result["last_error"] is not None

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
    async def test_container_failure_checkpoints_partial_handoff(self, tmp_path):
        """A returned container failure preserves its blocker handoff for another worker."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(workspace_path=str(tmp_path), handoffs={})
        mock_jira = _make_mock_jira()
        container_result = MagicMock(success=False, error_message="container failed")

        async def run_with_partial_handoff(**_kwargs):
            forge_dir = tmp_path / ".forge"
            forge_dir.mkdir()
            (forge_dir / "handoff.md").write_text("Partial work; blocked by failing test")
            return container_result

        runner = MagicMock(run=run_with_partial_handoff)
        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(state)

        assert result["last_error"] == "container failed"
        assert result["handoffs"]["acme/backend"]["content"].startswith("Partial work")
        assert result["handoffs"]["acme/backend"]["task_key"] == "TASK-456"

    @pytest.mark.asyncio
    async def test_runner_exception_checkpoints_handoff_written_before_crash(self, tmp_path):
        """A runner exception still captures a handoff already flushed to the workspace."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(workspace_path=str(tmp_path), handoffs={})
        mock_jira = _make_mock_jira()

        async def run_then_raise(**_kwargs):
            forge_dir = tmp_path / ".forge"
            forge_dir.mkdir()
            (forge_dir / "handoff.md").write_text("Timed out after partial implementation")
            raise TimeoutError("container timed out")

        runner = MagicMock(run=run_then_raise)
        with (
            patch("forge.workflow.nodes.implementation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.implementation.ContainerRunner", return_value=runner),
            patch("forge.workflow.nodes.implementation.get_settings"),
        ):
            result = await implement_task(state)

        assert result["last_error"] == "container timed out"
        assert result["handoffs"]["acme/backend"]["content"].startswith("Timed out")

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
    async def test_same_path_recreation_does_not_mark_pending_task_complete(self, tmp_path) -> None:
        """A deterministic path does not prove that the original clone survived."""
        from forge.workflow.nodes.implementation import implement_task

        state = _make_state(workspace_path=str(tmp_path))
        state["implementation_push_pending"] = True
        state["implementation_push_pending_task"] = "TASK-456"
        mock_git = MagicMock()
        mock_git.workspace_recreated = True
        mock_jira = _make_mock_jira()
        runner = _make_successful_runner()

        with (
            patch(
                "forge.workflow.nodes.implementation.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
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


class TestImplementTaskStepName:
    """Tests for step_name propagation to ContainerRunner."""

    @pytest.mark.asyncio
    async def test_passes_step_name_implement_task_to_runner(self):
        """ContainerRunner.run() is called with step_name='implement_task'."""
        from forge.workflow.nodes.implementation import implement_task

        mock_jira = _make_mock_jira()
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

        # Verify step_name was passed
        runner.run.assert_called_once()
        call_kwargs = runner.run.call_args.kwargs
        assert call_kwargs.get("step_name") == "implement_task"
