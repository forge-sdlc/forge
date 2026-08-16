"""Container runner for sandbox code execution.

This module handles spawning and managing podman containers for
AI-powered code implementation. The orchestrator uses this to:

1. Spawn a container with the workspace mounted
2. Wait for completion
3. Retrieve exit status and logs
4. Clean up the container

The container runs the entrypoint script which invokes Deep Agents
to implement tasks with full tool access.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.api.routes.metrics import (
    observe_review_duration,
    record_review_cycle,
    record_review_verdict,
)
from forge.config import Settings, get_settings
from forge.model_policy import resolve_model_target_for_project
from forge.models.model_policy import ResolvedModelTarget
from forge.observability import (
    ReviewCycleData,
    ReviewCyclePoller,
    ReviewCycleRecorder,
)
from forge.prompts import load_prompt
from forge.skills.resolver import resolve_skill_paths

logger = logging.getLogger(__name__)


def _process_cycle(
    cycle: ReviewCycleData,
    step_name: str,
    recorder: "ReviewCycleRecorder",
    collected_cycles: list[ReviewCycleData],
) -> None:
    """Record a review cycle: append, log via recorder, emit Prometheus metrics."""
    collected_cycles.append(cycle)
    recorder.record(cycle)
    if cycle.file_path:
        recorder.record_file(Path(cycle.file_path))
    record_review_cycle(cycle.skill, step_name)
    record_review_verdict(cycle.skill, step_name, cycle.verdict)
    observe_review_duration(cycle.skill, step_name, cycle.elapsed_seconds)


# Default container image (can be overridden via CONTAINER_IMAGE env var)
# Use localhost/ prefix to avoid podman short-name resolution prompts
DEFAULT_IMAGE = "localhost/forge-dev:latest"

# Exit codes from entrypoint.py
EXIT_SUCCESS = 0
EXIT_TASK_FAILED = 1
EXIT_TESTS_FAILED = 2
EXIT_CONFIG_ERROR = 3


@dataclass
class ContainerResult:
    """Result from container execution."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    tests_passed: bool | None = None  # None if tests were skipped
    error_message: str | None = None
    review_cycles: list[ReviewCycleData] = field(default_factory=list)

    @property
    def tests_failed(self) -> bool:
        """Check if tests specifically failed."""
        return self.exit_code == EXIT_TESTS_FAILED

    @property
    def review_exhausted(self) -> bool:
        """Check if the review loop exhausted all retries without approval."""
        if not self.review_cycles:
            return False
        last = self.review_cycles[-1]
        return last.verdict == "rejected" and last.cycle >= last.max_cycles


@dataclass
class ContainerConfig:
    """Configuration for container execution."""

    image: str = DEFAULT_IMAGE
    timeout_seconds: int = 1800  # 30 minutes default
    memory_limit: str = "4g"
    cpu_limit: str = "2"
    network_mode: str = "slirp4netns"  # Rootless networking
    skip_tests: bool = False
    max_retries: int = 3
    env_vars: dict[str, str] = field(default_factory=dict)


class ContainerRunner:
    """Manages container lifecycle for sandbox execution.

    This class provides the interface between the Forge orchestrator
    and podman containers. It handles:

    - Container spawning with proper mounts and limits
    - Passing credentials securely via environment
    - Waiting for completion with timeout
    - Capturing logs and exit status
    - Container cleanup
    """

    def __init__(self, settings: Settings | None = None):
        """Initialize the container runner.

        Args:
            settings: Application settings. Uses default if not provided.
        """
        self.settings = settings or get_settings()
        self._verify_podman()

    def _verify_podman(self) -> None:
        """Verify podman is available."""
        if not shutil.which("podman"):
            raise RuntimeError("podman not found in PATH")

    def _default_config(self) -> ContainerConfig:
        """Create default config from settings."""
        return ContainerConfig(
            image=self.settings.container_image,
            timeout_seconds=self.settings.container_timeout,
            memory_limit=self.settings.container_memory,
            cpu_limit=self.settings.container_cpus,
        )

    def _build_env_vars(
        self,
        config: ContainerConfig,
        container_skill_paths: str = "",
        model_target: ResolvedModelTarget | None = None,
    ) -> dict[str, str]:
        """Build environment variables to pass to container.

        Args:
            config: Container configuration.
            container_skill_paths: Skill paths inside container (from _get_skill_mounts).

        Returns:
            Dict of environment variables.
        """
        env = {}

        selected_backend = model_target.backend if model_target else self.settings.llm_backend
        if not selected_backend:
            raise ValueError("llm_backend must be configured before building container env")
        env["LLM_BACKEND"] = selected_backend

        if selected_backend == "google-genai":
            google_api_key = self.settings.google_api_key.get_secret_value()
            if google_api_key:
                env["GOOGLE_API_KEY"] = google_api_key
        elif selected_backend == "anthropic":
            anthropic_api_key = self.settings.anthropic_api_key.get_secret_value()
            if anthropic_api_key:
                env["ANTHROPIC_API_KEY"] = anthropic_api_key

        # Pass Vertex AI credentials
        if selected_backend == "vertex-ai":
            env["GOOGLE_CLOUD_PROJECT"] = (
                model_target.project if model_target else self.settings.google_cloud_project
            ) or ""
            env["GOOGLE_CLOUD_LOCATION"] = (
                model_target.location if model_target else self.settings.google_cloud_location
            ) or "global"
            # GOOGLE_APPLICATION_CREDENTIALS will be set if we mount gcloud creds
            env["GOOGLE_APPLICATION_CREDENTIALS"] = (
                "/root/.config/gcloud/application_default_credentials.json"
            )

        # Pass model configuration
        # Use container-specific model if configured, otherwise fall back to default
        env["LLM_MODEL"] = model_target.model if model_target else self.settings.container_model
        env["LLM_MAX_TOKENS"] = str(
            model_target.max_output_tokens
            if model_target and model_target.max_output_tokens
            else self.settings.llm_max_tokens
        )
        if model_target and model_target.temperature is not None:
            env["LLM_TEMPERATURE"] = str(model_target.temperature)
        if model_target:
            env["FORGE_MODEL_CONNECTION"] = model_target.connection
            env["FORGE_MODEL_POLICY_KEY"] = model_target.policy_key
            env["FORGE_MODEL_POLICY_SOURCE"] = model_target.policy_source

        # Pass skill paths for agent (only if explicitly configured)
        if container_skill_paths:
            env["AGENT_SKILL_PATHS"] = container_skill_paths

        # Pass git configuration for commits
        env["GIT_USER_NAME"] = self.settings.git_user_name
        env["GIT_USER_EMAIL"] = self.settings.git_user_email
        env["CONTAINER_COMMAND_TIMEOUT"] = str(self.settings.container_command_timeout)

        # Pass Langfuse tracing credentials if enabled
        if self.settings.langfuse_enabled:
            env["LANGFUSE_PUBLIC_KEY"] = self.settings.langfuse_public_key
            env["LANGFUSE_SECRET_KEY"] = self.settings.langfuse_secret_key.get_secret_value()
            env["LANGFUSE_HOST"] = self.settings.langfuse_host
            env["LANGFUSE_TRACE_TAGS"] = self.settings.langfuse_trace_tags
            env["LANGFUSE_TRACE_METADATA"] = self.settings.langfuse_trace_metadata
            logger.debug("Container Langfuse tracing enabled")

        # Pass system prompt template (unformatted - entrypoint will interpolate)
        # Load raw template without interpolation by passing empty values
        prompt_template = load_prompt("container-system")
        env["FORGE_SYSTEM_PROMPT_TEMPLATE"] = prompt_template

        # Pass debug/verbose settings for container agent
        if self.settings.container_langchain_verbose:
            env["LANGCHAIN_VERBOSE"] = "true"
        # Pass log level from settings
        env["LOG_LEVEL"] = self.settings.log_level

        # Merge with any custom env vars from config
        env.update(config.env_vars)

        return env

    def _get_gcloud_credentials_path(self) -> Path | None:
        """Get path to gcloud application default credentials if they exist."""
        env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if env_path:
            p = Path(env_path)
            if p.exists():
                return p
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc_path.exists():
            return adc_path
        return None

    def _get_skill_mounts(
        self, ticket_key: str | None = None
    ) -> tuple[list[tuple[Path, str]], str]:
        """Get skill directory mounts and container paths.

        Resolves skill directories via the resolver in ascending priority:
        committed defaults (``skills_dir/default/``), committed per-project
        overrides (``skills_dir/{project}/``), and runtime-fetched project
        skills (``skills_install_dir/{project}/``), with later sources winning.

        Returns:
            Tuple of (mounts, container_paths) where:
            - mounts: List of (host_path, container_path) tuples
            - container_paths: Comma-separated paths for AGENT_SKILL_PATHS env var
        """
        skills_dir = Path.cwd() / self.settings.skills_dir.rstrip("/")
        # Assumes worker and runner share the same host filesystem — skills_install_dir
        # is populated by the worker and mounted into the container from the host.
        host_paths = [
            Path(p.rstrip("/"))
            for p in resolve_skill_paths(
                ticket_key or "", skills_dir, skills_install_dir=self.settings.skills_install_dir
            )
        ]

        mounts = []
        container_paths = []

        for i, host_path in enumerate(host_paths):
            if not host_path.is_absolute():
                host_path = Path.cwd() / host_path

            if not host_path.exists():
                logger.warning(f"Skill path does not exist: {host_path}")
                continue

            container_path = f"/skills/skill_{i}"
            mounts.append((host_path.resolve(), container_path))
            container_paths.append(f"{container_path}/")
            logger.info(f"Mounting skill dir: {host_path} → {container_path}")

        return mounts, ",".join(container_paths)

    def _build_container_name(
        self,
        ticket_key: str | None = None,
        _repo_name: str | None = None,
    ) -> str:
        """Build container name for identification.

        Format: forge-{ticket}-{uid} e.g., forge-AISOS-189-a1b2c3
        Uses a unique suffix to avoid name collisions when multiple
        containers run for the same ticket (e.g., RCA → reflection → RCA).
        """
        import uuid

        name_parts = ["forge"]
        if ticket_key:
            name_parts.append(ticket_key)
        name_parts.append(uuid.uuid4().hex[:6])
        return "-".join(name_parts)

    def _build_podman_command(
        self,
        workspace_path: Path,
        task_file: Path,
        config: ContainerConfig,
        container_name: str,
        ticket_key: str | None = None,
        model_target: ResolvedModelTarget | None = None,
    ) -> list[str]:
        """Build the podman run command."""

        cmd = [
            "podman",
            "run",
            "--name",
            container_name,
        ]
        if not self.settings.container_keep:
            cmd.append("--rm")
        cmd += [
            # Mount workspace
            "-v",
            f"{workspace_path}:/workspace:Z",
            # Mount task file
            "-v",
            f"{task_file}:/task.json:ro,Z",
            # Resource limits
            "--memory",
            config.memory_limit,
            "--cpus",
            config.cpu_limit,
            # Network (limited)
            "--network",
            config.network_mode,
            # Working directory
            "-w",
            "/workspace",
        ]

        # Mount gcloud credentials for Vertex AI authentication
        selected_backend = model_target.backend if model_target else self.settings.llm_backend
        if selected_backend == "vertex-ai":
            gcloud_creds = self._get_gcloud_credentials_path()
            if gcloud_creds:
                # Mount the credentials file to container
                cmd.extend(
                    [
                        "-v",
                        f"{gcloud_creds}:/root/.config/gcloud/application_default_credentials.json:ro,Z",
                    ]
                )

        # Mount skill directories
        skill_mounts, container_skill_paths = self._get_skill_mounts(ticket_key)
        for host_path, container_path in skill_mounts:
            cmd.extend(["-v", f"{host_path}:{container_path}:ro,Z"])

        # Add environment variables
        for key, value in self._build_env_vars(config, container_skill_paths, model_target).items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add timeout
        cmd.extend(["--timeout", str(config.timeout_seconds)])

        # Add image
        cmd.append(config.image)

        # Add entrypoint arguments
        cmd.extend(
            [
                "--task-file",
                "/task.json",
                "--max-retries",
                str(config.max_retries),
            ]
        )

        if config.skip_tests:
            cmd.append("--skip-tests")

        return cmd

    async def _stop_timed_out_container(
        self,
        container_name: str,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Stop a running container and ensure the podman run process exits."""
        stop_process = await asyncio.create_subprocess_exec(
            "podman",
            "stop",
            "-t",
            "10",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        should_kill = False
        try:
            await asyncio.wait_for(stop_process.wait(), timeout=15.0)
            if stop_process.returncode != 0:
                logger.warning(
                    f"podman stop failed for {container_name} "
                    f"(exit {stop_process.returncode}), killing"
                )
                should_kill = True
        except TimeoutError:
            logger.warning(f"Container {container_name} didn't stop, killing")
            should_kill = True

        if should_kill:
            kill_process = await asyncio.create_subprocess_exec(
                "podman",
                "kill",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(kill_process.wait(), timeout=15.0)
            except TimeoutError:
                logger.warning(f"podman kill for {container_name} did not finish")

        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except TimeoutError:
            logger.warning(f"podman run process for {container_name} did not exit, killing")
            process.kill()
            await process.wait()

    def _sweep_review_cycles(
        self,
        workspace_path: Path,
        step_name: str,
        processed_files: set[str],
        collected_cycles: list[ReviewCycleData],
        recorder: ReviewCycleRecorder,
        task_key: str = "",
        skill_name: str = "",
    ) -> None:
        """Synchronous post-execution sweep for missed review cycle files.

        This method scans for any review_cycle_*.json files that may have been
        missed during async polling, especially if the container exits quickly
        after writing.

        Args:
            workspace_path: Path to the workspace root.
            step_name: Name of the step for metrics.
            processed_files: Set of file paths already processed by the poller.
            collected_cycles: List to append newly found cycles to.
            recorder: Recorder for logging/copying detected cycles.
            task_key: Jira task key for directory naming.
            skill_name: Skill name for directory naming.
        """
        cycle_dir = ReviewCyclePoller.build_cycle_dir(
            workspace_path, task_key, skill_name, step_name
        )
        if not cycle_dir.exists():
            return

        # Find all review cycle files
        all_files = sorted(cycle_dir.glob("review_cycle_*.json"))

        missed_count = 0
        for file_path in all_files:
            file_key = str(file_path)

            # Skip files already processed by the async poller
            if file_key in processed_files:
                continue

            # This file was missed during polling - parse and collect it
            try:
                content = file_path.read_text(encoding="utf-8")
                if not content.strip():
                    logger.warning("Empty review cycle file during sweep: %s", file_path)
                    continue

                data = json.loads(content)
                cycle_data = ReviewCycleData.from_dict(data, file_path=file_key)
                missed_count += 1
                _process_cycle(cycle_data, step_name, recorder, collected_cycles)
                logger.debug(
                    "Sweep caught review cycle %d/%d for %s: %s",
                    cycle_data.cycle,
                    cycle_data.max_cycles,
                    step_name,
                    cycle_data.verdict,
                )

            except json.JSONDecodeError as e:
                logger.warning("Failed to parse review cycle file %s: %s", file_path, e)
            except (KeyError, TypeError) as e:
                logger.warning("Invalid review cycle data in %s: %s", file_path, e)
            except OSError as e:
                logger.warning("Error reading review cycle file %s: %s", file_path, e)

        if missed_count > 0:
            logger.warning(
                "Sweep caught %d review cycle file(s) missed during async polling for step %s",
                missed_count,
                step_name,
            )

    async def _poll_review_cycles(
        self,
        poller: ReviewCyclePoller,
        recorder: ReviewCycleRecorder,
        collected_cycles: list[ReviewCycleData],
    ) -> None:
        """Background task to poll for review cycle files during container execution.

        This task polls the workspace for review_cycle_*.json files and:
        - Collects detected ReviewCycleData into the provided list
        - Records cycles via the recorder (log or copy mode)
        - Emits Prometheus metrics for observability

        Args:
            poller: The ReviewCyclePoller instance to use for polling.
            recorder: The ReviewCycleRecorder for recording cycles.
            collected_cycles: List to aggregate detected cycles into.
        """

        def on_cycles(new_cycles: list[ReviewCycleData]) -> None:
            for cycle in new_cycles:
                _process_cycle(cycle, poller.step_name, recorder, collected_cycles)

        try:
            await poller.run_loop(on_cycles)
        except asyncio.CancelledError:
            logger.debug("Review polling task cancelled")
            raise

    async def _start_review_polling(
        self,
        workspace_path: Path,
        step_name: str | None,
        task_key: str,
        skill_name: str,
        collected_cycles: list[ReviewCycleData],
    ) -> tuple[ReviewCyclePoller | None, ReviewCycleRecorder | None, asyncio.Task | None]:
        """Create review poller, recorder, and start background polling task.

        Args:
            workspace_path: Path to the workspace root.
            step_name: Workflow step name for organizing review files.
                If not provided, polling is disabled and (None, None, None) is returned.
            task_key: Jira task key for directory naming.
            skill_name: Skill name for directory naming.
            collected_cycles: List to aggregate detected cycles into.

        Returns:
            Tuple of (poller, recorder, polling_task), or (None, None, None)
            if step_name is not provided.
        """
        if not step_name:
            return None, None, None

        poller = ReviewCyclePoller(
            workspace_path=workspace_path,
            step_name=step_name,
            task_key=task_key,
            skill_name=skill_name,
            settings=self.settings,
        )
        record_mode = self.settings.auto_review_record_polled_files
        if record_mode == "copy":
            logger.warning(
                "Review recording mode 'copy' is not yet supported "
                "(no recording_dir configured), falling back to 'log'"
            )
            record_mode = "log"
        recorder = ReviewCycleRecorder(
            step_name=step_name,
            mode=record_mode,
            recording_dir=None,
        )
        polling_task = asyncio.create_task(
            self._poll_review_cycles(poller, recorder, collected_cycles)
        )
        logger.debug(f"Started review polling for step: {step_name}")
        return poller, recorder, polling_task

    @staticmethod
    def _clear_stale_review_cycles(
        workspace_path: Path,
        step_name: str | None,
        task_key: str,
        skill_name: str,
    ) -> None:
        """Remove review artifacts from an earlier execution.

        This runs before the container is launched so files produced by the
        current execution cannot be mistaken for stale artifacts and deleted.
        A disabled review, missing directory, or empty directory is a no-op.
        """
        if not step_name:
            return

        cycle_dir = ReviewCyclePoller.build_cycle_dir(
            workspace_path, task_key, skill_name, step_name
        )
        if not cycle_dir.is_dir():
            return

        for stale_file in cycle_dir.glob("review_cycle_*.json"):
            stale_file.unlink()
            logger.debug("Cleared stale review cycle file: %s", stale_file)

    async def _finalize_review_polling(
        self,
        poller: ReviewCyclePoller | None,
        recorder: ReviewCycleRecorder | None,
        polling_task: asyncio.Task | None,
        workspace_path: Path,
        step_name: str | None,
        task_key: str,
        skill_name: str,
        collected_cycles: list[ReviewCycleData],
    ) -> None:
        """Stop review poller, cancel polling task, and sweep for missed files.

        Args:
            poller: The ReviewCyclePoller instance, or None if polling was disabled.
            recorder: The ReviewCycleRecorder instance, or None if polling was disabled.
            polling_task: The background polling asyncio.Task, or None if polling was disabled.
            workspace_path: Path to the workspace root.
            step_name: Workflow step name for organizing review files.
            task_key: Jira task key for directory naming.
            skill_name: Skill name for directory naming.
            collected_cycles: List to aggregate detected cycles into.
        """
        if not polling_task or not poller or not recorder or not step_name:
            return

        # Stop the poller
        poller.stop()
        # Cancel the polling task
        polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling_task
        logger.debug("Review polling task stopped")

        # Do one final async poll to catch any remaining files
        final_cycles = await poller.poll_once()
        for cycle in final_cycles:
            _process_cycle(cycle, poller.step_name, recorder, collected_cycles)

        # Synchronous sweep for any files missed during async polling
        # This catches files written just before container exit that may
        # not have been detected by the async poller
        self._sweep_review_cycles(
            workspace_path=workspace_path,
            step_name=step_name,
            processed_files=poller._processed_files,
            collected_cycles=collected_cycles,
            recorder=recorder,
            task_key=task_key,
            skill_name=skill_name,
        )

    def _build_container_result(
        self,
        exit_code: int,
        stdout_str: str,
        stderr_str: str,
        collected_cycles: list[ReviewCycleData],
        container_name: str,
    ) -> ContainerResult:
        """Map container exit code to a ContainerResult.

        Handles logging of container output at appropriate levels and
        emits the container_keep debugging warning when applicable.

        Args:
            exit_code: Process exit code.
            stdout_str: Decoded container stdout.
            stderr_str: Decoded container stderr.
            collected_cycles: Review cycles collected during execution.
            container_name: Container name for log messages.

        Returns:
            ContainerResult reflecting the exit status.
        """
        logger.info(f"Container exited with code {exit_code}")

        # Log container output
        if exit_code != EXIT_SUCCESS:
            # Failure: stderr at INFO, stdout at DEBUG
            if stderr_str:
                logger.info(f"Container stderr:\n{stderr_str}")
            if stdout_str:
                logger.debug(f"Container stdout:\n{stdout_str}")
            if self.settings.container_keep:
                logger.warning(
                    f"Container kept for debugging (FORGE_CONTAINER_KEEP=true): "
                    f"{container_name}\n"
                    f"  Inspect logs:      podman logs {container_name}\n"
                    f"  Enter filesystem:  podman export {container_name} | tar -xC /tmp/{container_name}\n"
                    f"  Remove when done:  podman rm {container_name}"
                )
        else:
            # Success: stderr at DEBUG only
            if stderr_str:
                logger.debug(f"Container stderr:\n{stderr_str}")

        # Determine result
        if exit_code == EXIT_SUCCESS:
            return ContainerResult(
                success=True,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                tests_passed=True,
                review_cycles=collected_cycles,
            )
        elif exit_code == EXIT_TESTS_FAILED:
            return ContainerResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                tests_passed=False,
                error_message="Tests failed after max retries",
                review_cycles=collected_cycles,
            )
        else:
            return ContainerResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout_str,
                stderr=stderr_str,
                error_message=f"Task failed with exit code {exit_code}",
                review_cycles=collected_cycles,
            )

    async def run(
        self,
        workspace_path: Path,
        task_summary: str,
        task_description: str,
        config: ContainerConfig | None = None,
        ticket_key: str | None = None,
        task_key: str | None = None,
        repo_name: str | None = None,
        previous_task_keys: list[str] | None = None,
        trace_context: dict[str, Any] | None = None,
        step_name: str | None = None,
        skill_name: str | None = None,
        model_target: ResolvedModelTarget | None = None,
        policy_key: str | None = None,
    ) -> ContainerResult:
        """Run a task in a container sandbox.

        Args:
            workspace_path: Path to the cloned repository workspace.
            task_summary: Short task summary.
            task_description: Detailed task description.
            config: Container configuration. Uses defaults if not provided.
            ticket_key: Jira ticket key for container naming (the Feature/Epic).
            task_key: Jira task key being implemented.
            repo_name: Repository name (e.g., "owner/repo") for container naming.
            previous_task_keys: List of previously implemented task keys for handoff context.
            trace_context: Workflow fields forwarded to Langfuse only.
            step_name: Workflow step name (e.g., "implement_task", "local_review")
                for organizing review cycle files under .forge/{step-name}/.
                If not provided, review polling is disabled.

        Returns:
            ContainerResult with execution status, logs, and review_cycles.
        """
        config = config or self._default_config()
        project_key = (
            ticket_key.split("-", 1)[0].upper() if ticket_key and "-" in ticket_key else None
        )
        if model_target is None and policy_key:
            model_target = await resolve_model_target_for_project(
                self.settings, project_key, policy_key
            )

        # Create task file in .forge directory (excluded from commits)
        forge_dir = workspace_path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        task_file = forge_dir / "task.json"
        resolved_trace_context = dict(trace_context or {})
        if model_target:
            resolved_trace_context.update(model_target.trace_metadata())
        task_data = {
            "task_key": task_key or "UNKNOWN",
            "summary": task_summary,
            "description": task_description,
            "previous_task_keys": previous_task_keys or [],
            "trace_context": resolved_trace_context,
            "skill_name": skill_name or "",
            "model_target": model_target.model_dump(mode="json") if model_target else {},
        }
        task_file.write_text(json.dumps(task_data, indent=2))

        # List to collect review cycles detected during execution
        collected_cycles: list[ReviewCycleData] = []
        poller: ReviewCyclePoller | None = None
        recorder: ReviewCycleRecorder | None = None
        polling_task: asyncio.Task | None = None

        try:
            # Build container name and command
            container_name = self._build_container_name(ticket_key, repo_name)
            cmd = self._build_podman_command(
                workspace_path, task_file, config, container_name, ticket_key, model_target
            )

            logger.info(f"Starting container {container_name} for task: {task_summary}")
            logger.debug(f"Command: {' '.join(cmd)}")

            # Clear artifacts from a prior execution before the container can
            # write review cycles for this execution.
            self._clear_stale_review_cycles(
                workspace_path,
                step_name,
                task_key or "",
                skill_name or "",
            )

            # Run container
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Start review polling background task if step_name is provided
            poller, recorder, polling_task = await self._start_review_polling(
                workspace_path,
                step_name,
                task_key or "",
                skill_name or "",
                collected_cycles,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=config.timeout_seconds + 60,  # Extra buffer
                )
            except TimeoutError:
                logger.error(f"Container execution timed out, stopping {container_name}")
                await self._stop_timed_out_container(container_name, process)
                return ContainerResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr="Container execution timed out",
                    error_message="Timeout exceeded",
                    review_cycles=collected_cycles,
                )
            except asyncio.CancelledError:
                logger.warning(f"Container execution cancelled, stopping {container_name}")
                await self._stop_timed_out_container(container_name, process)
                raise  # Re-raise CancelledError
            finally:
                await self._finalize_review_polling(
                    poller,
                    recorder,
                    polling_task,
                    workspace_path,
                    step_name,
                    task_key or "",
                    skill_name or "",
                    collected_cycles,
                )

            exit_code = process.returncode or 0
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            return self._build_container_result(
                exit_code, stdout_str, stderr_str, collected_cycles, container_name
            )

        finally:
            # Cleanup task file
            if task_file.exists():
                task_file.unlink()

    async def build_image(
        self,
        containerfile_path: Path | None = None,
        tag: str = DEFAULT_IMAGE,
    ) -> bool:
        """Build the container image.

        Args:
            containerfile_path: Path to Containerfile. Uses default if not provided.
            tag: Image tag. Defaults to forge-dev:latest.

        Returns:
            True if build succeeded.
        """
        if containerfile_path is None:
            # Find Containerfile in project
            project_root = Path(__file__).parent.parent.parent.parent
            containerfile_path = project_root / "containers" / "Containerfile"

        if not containerfile_path.exists():
            logger.error(f"Containerfile not found: {containerfile_path}")
            return False

        context_dir = containerfile_path.parent

        cmd = [
            "podman",
            "build",
            "-t",
            tag,
            "-f",
            str(containerfile_path),
            str(context_dir),
        ]

        logger.info(f"Building container image: {tag}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info(f"Successfully built image: {tag}")
            return True
        else:
            logger.error(f"Failed to build image: {stderr.decode()}")
            return False

    async def image_exists(self, tag: str = DEFAULT_IMAGE) -> bool:
        """Check if the container image exists locally.

        Args:
            tag: Image tag to check.

        Returns:
            True if image exists.
        """
        cmd = ["podman", "image", "exists", tag]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await process.wait()
        return process.returncode == 0

    async def pull_base_image(self) -> bool:
        """Pull the devcontainers/universal base image.

        Returns:
            True if pull succeeded.
        """
        cmd = [
            "podman",
            "pull",
            "mcr.microsoft.com/devcontainers/universal:linux",
        ]

        logger.info("Pulling devcontainers/universal base image...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info("Successfully pulled base image")
            return True
        else:
            logger.error(f"Failed to pull base image: {stderr.decode()}")
            return False
