"""Tests for container runner timeout cleanup."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.sandbox.runner import ContainerConfig, ContainerRunner


def _runner_without_init() -> ContainerRunner:
    return object.__new__(ContainerRunner)


@pytest.mark.asyncio
async def test_stop_failure_kills_container_and_waits_for_run_process() -> None:
    runner = _runner_without_init()
    stop_process = MagicMock()
    stop_process.returncode = 1
    stop_process.wait = AsyncMock()
    kill_process = MagicMock()
    kill_process.wait = AsyncMock()
    run_process = MagicMock()
    run_process.wait = AsyncMock()
    run_process.kill = MagicMock()

    with patch(
        "forge.sandbox.runner.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[stop_process, kill_process]),
    ) as mock_exec:
        await runner._stop_timed_out_container("forge-ticket-abc123", run_process)

    assert mock_exec.call_count == 2
    assert mock_exec.call_args_list[0].args[:3] == ("podman", "stop", "-t")
    assert mock_exec.call_args_list[1].args[:2] == ("podman", "kill")
    run_process.wait.assert_awaited()
    run_process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_run_process_wait_timeout_kills_run_process() -> None:
    runner = _runner_without_init()
    stop_process = MagicMock()
    stop_process.returncode = 0
    stop_process.wait = AsyncMock()
    run_process = MagicMock()
    run_process.wait = AsyncMock()
    run_process.kill = MagicMock()
    calls = 0

    async def fake_wait_for(awaitable, timeout):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        if calls == 2:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    with (
        patch(
            "forge.sandbox.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=stop_process),
        ) as mock_exec,
        patch("forge.sandbox.runner.asyncio.wait_for", side_effect=fake_wait_for),
    ):
        await runner._stop_timed_out_container("forge-ticket-abc123", run_process)

    mock_exec.assert_awaited_once()
    run_process.kill.assert_called_once()
    assert run_process.wait.call_count == 2
    assert run_process.wait.await_count == 1


@pytest.mark.asyncio
async def test_run_writes_trace_context_to_task_file(tmp_path) -> None:
    runner = _runner_without_init()
    runner.settings = MagicMock()
    runner.settings.container_keep = False
    runner._build_container_name = MagicMock(return_value="forge-ticket-abc123")
    captured_task_data = {}

    def build_command(_workspace_path, task_file, *_args):  # noqa: ANN001
        captured_task_data.update(json.loads(task_file.read_text()))
        return ["podman", "run", "fake"]

    runner._build_podman_command = MagicMock(side_effect=build_command)

    process = MagicMock()
    process.communicate = AsyncMock(return_value=(b"ok", b""))
    process.returncode = 0

    trace_context = {
        "ticket_key": "FEAT-123",
        "ticket_type": "Feature",
        "current_node": "implement_task",
        "current_repo": "org/repo",
    }

    with patch(
        "forge.sandbox.runner.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        result = await runner.run(
            workspace_path=tmp_path,
            task_summary="Do it",
            task_description="Details",
            config=ContainerConfig(),
            ticket_key="FEAT-123",
            task_key="TASK-1",
            repo_name="org/repo",
            previous_task_keys=["TASK-0"],
            trace_context=trace_context,
        )

    assert result.success is True
    assert captured_task_data["trace_context"]["current_node"] == "implement_task"
    assert captured_task_data["trace_context"]["current_repo"] == "org/repo"
