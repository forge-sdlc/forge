"""Smoke test runner for verifying Forge core connectivity and execution."""

import contextlib
import logging
import operator
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from forge.config import Settings
from forge.orchestrator.checkpointer import clear_checkpoint, get_checkpointer
from forge.sandbox.runner import ContainerConfig, ContainerRunner

logger = logging.getLogger(__name__)


class SmokeTestState(TypedDict, total=False):
    """State for the minimal diagnostic LangGraph workflow."""

    passed_nodes: Annotated[list[str], operator.add]
    status: str


async def start_node(_state: SmokeTestState) -> dict:
    """Diagnostic graph start node."""
    return {"passed_nodes": ["start"], "status": "running"}


async def end_node(_state: SmokeTestState) -> dict:
    """Diagnostic graph end node."""
    return {"passed_nodes": ["end"], "status": "success"}


async def run_smoke_test(settings: Settings) -> int:
    """Run an end-to-end smoke test to verify key Forge runtime capabilities.

    Performs the verification in three sequential stages:
    1. Diagnostic Graph Execution
    2. Workspace Setup & Container Verification
    3. Container Agent Execution & Verification

    Returns:
        0 if all stages passed, or a nonzero exit code on failure.
    """
    stage1_status = "FAIL"
    stage2_status = "PENDING"
    stage3_status = "PENDING"

    stage1_err: Exception | None = None
    stage2_err: Exception | None = None
    stage3_err: Exception | None = None

    temp_dir: tempfile.TemporaryDirectory | None = None
    thread_id = f"smoke-test-graph-{uuid.uuid4().hex[:8]}"

    try:
        # ---------------------------------------------------------------------
        # Stage 1: Diagnostic Graph Execution
        # ---------------------------------------------------------------------
        try:
            logger.info("Starting Stage 1: Diagnostic Graph Execution")
            checkpointer = await get_checkpointer()

            workflow = StateGraph(SmokeTestState)
            workflow.add_node("start_node", start_node)
            workflow.add_node("end_node", end_node)
            workflow.add_edge(START, "start_node")
            workflow.add_edge("start_node", "end_node")
            workflow.add_edge("end_node", END)

            compiled_graph = workflow.compile(
                checkpointer=checkpointer,
                interrupt_after=["start_node"],
            )

            config = {"configurable": {"thread_id": thread_id}}
            interrupted_result = await compiled_graph.ainvoke(
                {"passed_nodes": [], "status": "initialized"}, config=config
            )
            if interrupted_result.get("status") != "running" or interrupted_result.get(
                "passed_nodes", []
            ) != ["start"]:
                raise RuntimeError(
                    "Diagnostic graph did not interrupt after the start node: "
                    f"status={interrupted_result.get('status')}, "
                    f"passed_nodes={interrupted_result.get('passed_nodes', [])}"
                )

            # Resume without supplying state so the graph must restore it from Redis.
            result = await compiled_graph.ainvoke(None, config=config)

            # Retrieve result and perform assertions
            final_status = result.get("status")
            final_passed_nodes = result.get("passed_nodes", [])

            if final_status != "success" or final_passed_nodes != ["start", "end"]:
                raise RuntimeError(
                    f"Unexpected diagnostic graph outcome: status={final_status}, "
                    f"passed_nodes={final_passed_nodes}"
                )

            # Cleanup state checkpoint
            await clear_checkpoint(thread_id)
            stage1_status = "PASS"
            logger.info("Stage 1: Diagnostic Graph Execution [PASS]")

        except Exception as e:
            stage1_status = "FAIL"
            stage1_err = e
            logger.error(f"Stage 1 failed: {e}", exc_info=True)
            with contextlib.suppress(Exception):
                await clear_checkpoint(thread_id)
            return 1

        # ---------------------------------------------------------------------
        # Stage 2: Workspace Setup & Container Verification
        # ---------------------------------------------------------------------
        try:
            stage2_status = "FAIL"
            logger.info("Starting Stage 2: Workspace Setup & Container Verification")

            if not shutil.which("podman"):
                raise RuntimeError("podman not found in system PATH")

            runner = ContainerRunner(settings=settings)

            # Verify that the configured image exists
            image_name = settings.container_image
            if not await runner.image_exists(image_name):
                raise RuntimeError(
                    f"Configured container image {image_name!r} does not exist locally"
                )

            # Create the disposable workspace directory
            temp_dir = tempfile.TemporaryDirectory()
            temp_dir_path = Path(temp_dir.name)

            # Initialize mock Git repository inside temporary workspace
            subprocess.run(["git", "init"], cwd=temp_dir_path, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.name", "Forge Smoke Test"],
                cwd=temp_dir_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "smoke-test@forge.sdlc"],
                cwd=temp_dir_path,
                check=True,
                capture_output=True,
            )

            # Write initial README.md
            readme_path = temp_dir_path / "README.md"
            readme_path.write_text(
                "# Forge Smoke Test Workspace\n\nThis is a temporary workspace for testing.",
                encoding="utf-8",
            )

            # Commit the readme to mock a git repository
            subprocess.run(
                ["git", "add", "README.md"], cwd=temp_dir_path, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "initial commit"],
                cwd=temp_dir_path,
                check=True,
                capture_output=True,
            )

            stage2_status = "PASS"
            logger.info("Stage 2: Workspace Setup & Container Verification [PASS]")

        except Exception as e:
            stage2_status = "FAIL"
            stage2_err = e
            logger.error(f"Stage 2 failed: {e}", exc_info=True)
            return 1

        # ---------------------------------------------------------------------
        # Stage 3: Container Agent Execution & Verification
        # ---------------------------------------------------------------------
        try:
            stage3_status = "FAIL"
            logger.info("Starting Stage 3: Container Agent Execution & Verification")

            # Configure container running config
            container_config = ContainerConfig(
                image=settings.container_image,
                timeout_seconds=300,
                skip_tests=True,
            )

            # Invoke the real container task runner in workspace
            result = await runner.run(
                workspace_path=temp_dir_path,
                task_summary="Smoke Test File Creation",
                task_description=(
                    "Create a text file in the workspace root named 'smoke_test_result.txt' "
                    "with content 'FORGE_SMOKE_TEST_PASSED'. Keep the change simple."
                ),
                config=container_config,
            )

            if not result.success:
                error_msg = result.error_message or "Container task failed"
                if result.stderr:
                    error_msg += f"\nStderr: {result.stderr}"
                raise RuntimeError(f"Container runner execution failed: {error_msg}")

            # Verify that the expected file was created and contains the correct result
            verification_file = temp_dir_path / "smoke_test_result.txt"
            if not verification_file.exists():
                raise RuntimeError(
                    "Expected file 'smoke_test_result.txt' was not created in workspace"
                )

            file_content = verification_file.read_text(encoding="utf-8").strip()
            if file_content != "FORGE_SMOKE_TEST_PASSED":
                raise RuntimeError(
                    f"Result file content mismatch: expected 'FORGE_SMOKE_TEST_PASSED', "
                    f"got {file_content!r}"
                )

            stage3_status = "PASS"
            logger.info("Stage 3: Container Agent Execution & Verification [PASS]")

        except Exception as e:
            stage3_status = "FAIL"
            stage3_err = e
            logger.error(f"Stage 3 failed: {e}", exc_info=True)
            return 1

        return 0

    finally:
        # Cleanup temporary workspace directories
        if temp_dir is not None:
            try:
                temp_dir.cleanup()
                logger.info("Temporary workspace directory successfully cleaned up.")
            except Exception as e:
                logger.warning(f"Failed to cleanly remove temporary directory: {e}")

        # Print clean checklists report of the run
        print("\n=================== Forge Smoke Test Report ===================")
        print(f"[{stage1_status}] Stage 1: Diagnostic Graph Execution")
        if stage1_err:
            print(f"        Error: {stage1_err}")
        print(f"[{stage2_status}] Stage 2: Workspace Setup & Container Verification")
        if stage2_err:
            print(f"        Error: {stage2_err}")
        print(f"[{stage3_status}] Stage 3: Container Agent Execution & Verification")
        if stage3_err:
            print(f"        Error: {stage3_err}")
        print("===============================================================\n")
