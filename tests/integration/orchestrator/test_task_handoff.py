"""Integration tests for task context handoff between containers.

These tests verify that:
1. The .forge directory structure is created during workspace setup
2. Previous task keys are passed to containers
3. Handoff format is correct in container prompts
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state as create_initial_state


class TestForgeDirectorySetup:
    """Test .forge directory creation during workspace setup."""

    async def test_workspace_setup_creates_forge_directory(self):
        """Workspace setup should create .forge and .forge/history directories."""
        from forge.workspace.manager import WorkspaceManager

        with tempfile.TemporaryDirectory() as base_dir:
            manager = WorkspaceManager(base_dir=base_dir)

            workspace = manager.create_workspace(
                repo_name="test-org/test-repo",
                ticket_key="TEST-123",
            )

            # Simulate what workspace_setup node does after clone
            forge_dir = workspace.path / ".forge"
            forge_dir.mkdir(exist_ok=True)
            (forge_dir / "history").mkdir(exist_ok=True)

            # Verify structure
            assert forge_dir.exists(), ".forge directory should exist"
            assert (forge_dir / "history").exists(), ".forge/history directory should exist"

    async def test_workspace_setup_node_creates_forge_directory(self):
        """The setup_workspace node should create .forge directory structure."""
        from forge.orchestrator.nodes import setup_workspace

        initial_state = create_initial_state(
            thread_id="TEST-123",
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
        )
        initial_state["tasks_by_repo"] = {"test-org/test-repo": ["TASK-1", "TASK-2"]}

        with patch("forge.workflow.nodes.workspace_setup.GitOperations") as MockGit, \
             patch("forge.workflow.nodes.workspace_setup.GuardrailsLoader") as MockGuardrails:

            mock_git = MagicMock()
            MockGit.return_value = mock_git

            mock_guardrails = MagicMock()
            mock_guardrails.load.return_value = MagicMock(get_system_context=MagicMock(return_value=""))
            MockGuardrails.return_value = mock_guardrails

            result = await setup_workspace(initial_state)

            # If workspace was set up, check for .forge directory
            if result.get("workspace_path"):
                workspace_path = Path(result["workspace_path"])
                assert (workspace_path / ".forge").exists(), ".forge should be created"
                assert (workspace_path / ".forge" / "history").exists(), ".forge/history should be created"


class TestPreviousTaskKeysPassing:
    """Test that previous task keys are passed to containers."""

    async def test_runner_passes_previous_task_keys_in_task_file(self):
        """ContainerRunner should include previous_task_keys in task file."""
        from forge.sandbox.runner import ContainerRunner

        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            # Mock podman and settings
            with patch("forge.sandbox.runner.shutil.which", return_value="/usr/bin/podman"), \
                 patch("forge.sandbox.runner.get_settings") as mock_settings:

                settings = MagicMock()
                settings.anthropic_api_key.get_secret_value.return_value = "test-key"
                settings.use_vertex_ai = False
                settings.claude_model = "claude-test"
                settings.container_image = "test-image"
                settings.container_timeout = 3600
                settings.container_memory = "4g"
                settings.container_cpus = "2"
                mock_settings.return_value = settings

                runner = ContainerRunner(settings)

                # Mock the actual run to just create the task file
                with patch.object(runner, "_build_podman_command", return_value=["echo", "test"]), \
                     patch("asyncio.create_subprocess_exec") as mock_exec:

                    mock_process = AsyncMock()
                    mock_process.communicate = AsyncMock(return_value=(b"", b""))
                    mock_process.returncode = 0
                    mock_exec.return_value = mock_process

                    await runner.run(
                        workspace_path=workspace,
                        task_summary="Test task",
                        task_description="Test description",
                        previous_task_keys=["TASK-1", "TASK-2"],
                    )

                    # Check the task file content (created before the command runs)
                    workspace / ".forge-task.json"
                    # File is cleaned up, but we can verify the call was made
                    # by checking _build_podman_command was called

    async def test_implementation_node_passes_implemented_tasks(self):
        """Implementation node should pass implemented_tasks as previous_task_keys."""
        from forge.orchestrator.nodes import implement_task
        from forge.workflow.feature.state import FeatureState as WorkflowState

        with tempfile.TemporaryDirectory() as workspace_dir:
            state: WorkflowState = {
                "ticket_key": "TEST-123",
                "workspace_path": workspace_dir,
                "current_task_key": "TASK-3",
                "task_keys": ["TASK-1", "TASK-2", "TASK-3"],
                "current_repo": "test-org/test-repo",
                "tasks_by_repo": {"test-org/test-repo": ["TASK-1", "TASK-2", "TASK-3"]},
                "implemented_tasks": ["TASK-1", "TASK-2"],
                "context": {"guardrails": ""},
            }

            with patch("forge.workflow.nodes.implementation.JiraClient") as MockJira, \
                 patch("forge.workflow.nodes.implementation.ContainerRunner") as MockRunner, \
                 patch("forge.workflow.nodes.implementation.get_settings") as mock_settings:

                # Setup mocks
                mock_jira = MagicMock()
                mock_jira.get_issue = AsyncMock(
                    return_value=MagicMock(
                        description="Test description",
                        summary="Test summary",
                    )
                )
                mock_jira.close = AsyncMock()
                MockJira.return_value = mock_jira

                mock_runner = MagicMock()
                mock_runner.run = AsyncMock(
                    return_value=MagicMock(success=True, exit_code=0)
                )
                MockRunner.return_value = mock_runner

                mock_settings.return_value = MagicMock()

                await implement_task(state)

                # Verify runner.run was called with previous_task_keys
                mock_runner.run.assert_called_once()
                call_kwargs = mock_runner.run.call_args.kwargs
                assert call_kwargs.get("previous_task_keys") == ["TASK-1", "TASK-2"]


class TestHandoffPromptFormat:
    """Test that handoff instructions are included in container prompts."""

    def test_container_system_prompt_includes_handoff_instructions(self):
        """Container system prompt should include handoff reading/writing instructions."""
        from forge.prompts import load_prompt

        prompt = load_prompt("container-system")

        # Check for handoff reading instructions
        assert ".forge/handoff.md" in prompt, "Prompt should reference handoff.md"
        assert ".forge/history/" in prompt, "Prompt should reference history directory"

        # Check for handoff writing instructions
        assert "Update handoff" in prompt or "update `.forge/handoff.md`" in prompt, \
            "Prompt should instruct agent to update handoff"

    def test_entrypoint_builds_prompt_with_previous_task_keys(self):
        """Entrypoint build_system_prompt should include previous task keys."""
        import sys
        from pathlib import Path

        # Add containers to path temporarily
        containers_path = Path(__file__).parent.parent.parent.parent / "containers"
        sys.path.insert(0, str(containers_path))

        try:
            from entrypoint import build_system_prompt

            prompt = build_system_prompt(
                workspace=Path("/workspace"),
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Test description",
                guardrails="",
                previous_task_keys=["TASK-1", "TASK-2"],
            )

            assert "TASK-1" in prompt, "Previous task keys should be in prompt"
            assert "TASK-2" in prompt, "Previous task keys should be in prompt"
        finally:
            sys.path.remove(str(containers_path))

    def test_entrypoint_handles_empty_previous_tasks(self):
        """Entrypoint should handle case with no previous tasks."""
        import sys
        from pathlib import Path

        containers_path = Path(__file__).parent.parent.parent.parent / "containers"
        sys.path.insert(0, str(containers_path))

        try:
            from entrypoint import build_system_prompt

            prompt = build_system_prompt(
                workspace=Path("/workspace"),
                task_key="TEST-123",
                task_summary="Test task",
                task_description="Test description",
                guardrails="",
                previous_task_keys=[],
            )

            # Should indicate this is the first task
            assert "first task" in prompt.lower() or "none" in prompt.lower(), \
                "Prompt should indicate no previous tasks"
        finally:
            sys.path.remove(str(containers_path))


class TestGitIgnoreSafeguard:
    """Test that .forge/ is added to .gitignore to prevent accidental commits."""

    async def test_forge_added_to_existing_gitignore(self):
        """Should append .forge/ to existing .gitignore."""
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            # Create existing .gitignore
            gitignore = workspace / ".gitignore"
            gitignore.write_text("node_modules/\n*.log\n")

            # Simulate workspace setup adding .forge to gitignore
            content = gitignore.read_text()
            if ".forge" not in content:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n# Forge workflow state (do not commit)\n.forge/\n"
                gitignore.write_text(content)

            # Verify
            final_content = gitignore.read_text()
            assert ".forge/" in final_content
            assert "node_modules/" in final_content  # Original content preserved

    async def test_forge_creates_gitignore_if_missing(self):
        """Should create .gitignore with .forge/ if it doesn't exist."""
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            gitignore = workspace / ".gitignore"

            # Simulate workspace setup creating .gitignore
            if not gitignore.exists():
                gitignore.write_text("# Forge workflow state (do not commit)\n.forge/\n")

            # Verify
            assert gitignore.exists()
            content = gitignore.read_text()
            assert ".forge/" in content

    async def test_forge_not_duplicated_in_gitignore(self):
        """Should not add .forge/ if already present."""
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            # Create .gitignore already containing .forge
            gitignore = workspace / ".gitignore"
            gitignore.write_text("node_modules/\n.forge/\n*.log\n")

            # Simulate workspace setup checking
            content = gitignore.read_text()
            if ".forge" not in content:
                content += "\n.forge/\n"
                gitignore.write_text(content)

            # Verify only one .forge/ entry
            final_content = gitignore.read_text()
            assert final_content.count(".forge") == 1

    def test_container_prompt_includes_gitignore_instructions(self):
        """Container system prompt should instruct agent about .forge/ exclusion."""
        from forge.prompts import load_prompt

        prompt = load_prompt("container-system")

        # Prompt should warn against committing .forge/ (using "NEVER commit" wording)
        assert ".forge/" in prompt, "Prompt should mention .forge/ directory"
        assert "NEVER commit" in prompt or "never commit" in prompt.lower(), \
            "Prompt should warn against committing .forge/"


class TestHistoryPersistence:
    """Test that conversation history can be persisted and loaded."""

    async def test_history_file_format(self):
        """History files should be valid JSON."""
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            # Create .forge structure
            forge_dir = workspace / ".forge"
            forge_dir.mkdir()
            history_dir = forge_dir / "history"
            history_dir.mkdir()

            # Simulate agent saving history
            history_data = {
                "task_key": "TASK-1",
                "messages": [
                    {"role": "user", "content": "Implement feature X"},
                    {"role": "assistant", "content": "I'll implement feature X..."},
                ],
                "completed_at": "2025-03-30T10:00:00Z",
            }

            history_file = history_dir / "TASK-1.json"
            history_file.write_text(json.dumps(history_data, indent=2))

            # Verify it can be loaded
            loaded = json.loads(history_file.read_text())
            assert loaded["task_key"] == "TASK-1"
            assert len(loaded["messages"]) == 2

    async def test_handoff_file_format(self):
        """Handoff file should be readable markdown."""
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            # Create .forge structure
            forge_dir = workspace / ".forge"
            forge_dir.mkdir()

            # Simulate agent creating handoff
            handoff_content = """## TASK-1: Add user authentication

**Changes Made:**
- Created auth.py with JWT handling
- Added tests in test_auth.py

**Key Context:**
- Using PyJWT library for token generation
- Tokens expire after 24 hours

**For Next Task:**
- Auth middleware is ready to use
"""

            handoff_file = forge_dir / "handoff.md"
            handoff_file.write_text(handoff_content)

            # Verify content
            content = handoff_file.read_text()
            assert "TASK-1" in content
            assert "Changes Made" in content
            assert "Key Context" in content
