"""Unit tests for the Forge smoke test CLI command runner."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.config import Settings
from forge.sandbox.runner import ContainerResult
from forge.smoke_test import run_smoke_test


@pytest.fixture
def mock_settings() -> Settings:
    """Fixture for settings used in tests."""
    return Settings(
        jira_base_url="https://mock-company.atlassian.net",
        jira_api_token="mock-token",
        jira_user_email="mock-email@company.com",
        github_token="mock-token",
        container_image="localhost/forge-dev:latest",
    )


@pytest.mark.asyncio
async def test_smoke_test_success(mock_settings: Settings) -> None:
    """Test successful execution of all three stages of the smoke test."""
    # 1. Mock Stage 1: Diagnostic Graph Execution
    mock_compiled_graph = MagicMock()
    mock_compiled_graph.ainvoke = AsyncMock(
        side_effect=lambda state, **_kwargs: (
            {"passed_nodes": ["start"], "status": "running"}
            if state is not None
            else {"passed_nodes": ["start", "end"], "status": "success"}
        )
    )

    # 2. Mock Stage 2: Workspace Setup & Container Verification
    async def mock_image_exists(_tag: str) -> bool:
        return True

    # 3. Mock Stage 3: Container Agent Execution & Verification
    async def mock_runner_run(workspace_path: Path, *_args, **_kwargs) -> ContainerResult:
        # Simulate container writing the expected verification file
        (workspace_path / "smoke_test_result.txt").write_text(
            "FORGE_SMOKE_TEST_PASSED", encoding="utf-8"
        )
        return ContainerResult(success=True, exit_code=0, stdout="Done", stderr="")

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock) as mock_clear,
        patch("shutil.which", return_value="/usr/bin/podman"),
        patch("forge.sandbox.runner.ContainerRunner.image_exists", side_effect=mock_image_exists),
        patch("forge.sandbox.runner.ContainerRunner.run", side_effect=mock_runner_run),
    ):
        exit_code = await run_smoke_test(mock_settings)

        assert exit_code == 0
        mock_clear.assert_called_once()
        assert mock_compiled_graph.ainvoke.await_count == 2
        assert mock_compiled_graph.ainvoke.await_args_list[1].args == (None,)


@pytest.mark.asyncio
async def test_smoke_test_graph_failure(mock_settings: Settings) -> None:
    """Test that a failure in Stage 1 aborts early and returns a nonzero exit code."""
    mock_compiled_graph = MagicMock()
    mock_compiled_graph.ainvoke = AsyncMock(
        side_effect=Exception("Failed to invoke diagnostic graph")
    )

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock) as mock_clear,
    ):
        exit_code = await run_smoke_test(mock_settings)

        assert exit_code == 1
        mock_clear.assert_called_once()  # clear_checkpoint should be called in finally/except block


@pytest.mark.asyncio
async def test_smoke_test_missing_podman_or_image(mock_settings: Settings) -> None:
    """Test that Stage 2 fails if podman is missing or the container image doesn't exist."""
    # Mock Stage 1 success
    mock_compiled_graph = MagicMock()
    mock_compiled_graph.ainvoke = AsyncMock(
        side_effect=lambda state, **_kwargs: (
            {"passed_nodes": ["start"], "status": "running"}
            if state is not None
            else {"passed_nodes": ["start", "end"], "status": "success"}
        )
    )

    # Case A: podman is missing
    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock),
        patch("shutil.which", return_value=None),
    ):
        exit_code = await run_smoke_test(mock_settings)
        assert exit_code == 1

    # Case B: container image does not exist
    async def mock_image_exists_false(_tag: str) -> bool:
        return False

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock),
        patch("shutil.which", return_value="/usr/bin/podman"),
        patch(
            "forge.sandbox.runner.ContainerRunner.image_exists", side_effect=mock_image_exists_false
        ),
    ):
        exit_code = await run_smoke_test(mock_settings)
        assert exit_code == 1


@pytest.mark.asyncio
async def test_smoke_test_agent_failure(mock_settings: Settings) -> None:
    """Test that Stage 3 fails if the container runner indicates task failure or omits the result."""
    # Mock Stage 1 success
    mock_compiled_graph = MagicMock()
    mock_compiled_graph.ainvoke = AsyncMock(
        side_effect=lambda state, **_kwargs: (
            {"passed_nodes": ["start"], "status": "running"}
            if state is not None
            else {"passed_nodes": ["start", "end"], "status": "success"}
        )
    )

    # Mock Stage 2 success
    async def mock_image_exists(_tag: str) -> bool:
        return True

    # Case A: container runner returns a failed ContainerResult
    async def mock_runner_run_fail(_workspace_path: Path, *_args, **_kwargs) -> ContainerResult:
        return ContainerResult(success=False, exit_code=1, stdout="", stderr="Compilation error")

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock),
        patch("shutil.which", return_value="/usr/bin/podman"),
        patch("forge.sandbox.runner.ContainerRunner.image_exists", side_effect=mock_image_exists),
        patch("forge.sandbox.runner.ContainerRunner.run", side_effect=mock_runner_run_fail),
    ):
        exit_code = await run_smoke_test(mock_settings)
        assert exit_code == 1

    # Case B: container runner succeeds but file is missing
    async def mock_runner_run_no_file(_workspace_path: Path, *_args, **_kwargs) -> ContainerResult:
        return ContainerResult(success=True, exit_code=0, stdout="Success", stderr="")

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock),
        patch("shutil.which", return_value="/usr/bin/podman"),
        patch("forge.sandbox.runner.ContainerRunner.image_exists", side_effect=mock_image_exists),
        patch("forge.sandbox.runner.ContainerRunner.run", side_effect=mock_runner_run_no_file),
    ):
        exit_code = await run_smoke_test(mock_settings)
        assert exit_code == 1


@pytest.mark.asyncio
async def test_smoke_test_timeout_and_cleanup(mock_settings: Settings) -> None:
    """Test that temporary workspaces are fully cleaned up even if container runner raises TimeoutError."""
    # Mock Stage 1 success
    mock_compiled_graph = MagicMock()
    mock_compiled_graph.ainvoke = AsyncMock(
        side_effect=lambda state, **_kwargs: (
            {"passed_nodes": ["start"], "status": "running"}
            if state is not None
            else {"passed_nodes": ["start", "end"], "status": "success"}
        )
    )

    # Mock Stage 2 success
    async def mock_image_exists(_tag: str) -> bool:
        return True

    # Mock Stage 3 raising TimeoutError
    async def mock_runner_run_timeout(*_args, **_kwargs) -> ContainerResult:
        raise TimeoutError("Execution timed out")

    # Spy on TemporaryDirectory.cleanup using a custom subclass or patching
    cleanup_called = False
    from tempfile import TemporaryDirectory as RealTemporaryDirectory

    class SpyTemporaryDirectory:
        def __init__(self, *_args, **_kwargs):
            self._td = RealTemporaryDirectory(*_args, **_kwargs)
            self.name = self._td.name

        def cleanup(self):
            nonlocal cleanup_called
            cleanup_called = True
            self._td.cleanup()

    with (
        patch("langgraph.graph.StateGraph.compile", return_value=mock_compiled_graph),
        patch("forge.smoke_test.get_checkpointer", new_callable=AsyncMock),
        patch("forge.smoke_test.clear_checkpoint", new_callable=AsyncMock),
        patch("shutil.which", return_value="/usr/bin/podman"),
        patch("forge.sandbox.runner.ContainerRunner.image_exists", side_effect=mock_image_exists),
        patch("tempfile.TemporaryDirectory", side_effect=SpyTemporaryDirectory),
        patch("forge.sandbox.runner.ContainerRunner.run", side_effect=mock_runner_run_timeout),
    ):
        exit_code = await run_smoke_test(mock_settings)
        assert exit_code == 1
        assert cleanup_called is True
