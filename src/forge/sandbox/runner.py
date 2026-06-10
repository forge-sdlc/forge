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
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from forge.config import Settings, get_settings
from forge.prompts import load_prompt
from forge.skills.resolver import resolve_skill_paths

logger = logging.getLogger(__name__)

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

    @property
    def tests_failed(self) -> bool:
        """Check if tests specifically failed."""
        return self.exit_code == EXIT_TESTS_FAILED


@dataclass
class ContainerConfig:
    """Configuration for container execution."""

    image: str = DEFAULT_IMAGE
    timeout_seconds: int = 7200  # 2 hours default
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
        self, config: ContainerConfig, container_skill_paths: str = ""
    ) -> dict[str, str]:
        """Build environment variables to pass to container.

        Args:
            config: Container configuration.
            container_skill_paths: Skill paths inside container (from _get_skill_mounts).

        Returns:
            Dict of environment variables.
        """
        env = {}

        # Pass Anthropic credentials
        if self.settings.anthropic_api_key.get_secret_value():
            env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key.get_secret_value()

        # Pass Vertex AI credentials
        if self.settings.use_vertex_ai:
            env["ANTHROPIC_VERTEX_PROJECT_ID"] = self.settings.anthropic_vertex_project_id
            env["ANTHROPIC_VERTEX_REGION"] = self.settings.anthropic_vertex_region
            # GOOGLE_APPLICATION_CREDENTIALS will be set if we mount gcloud creds
            env["GOOGLE_APPLICATION_CREDENTIALS"] = (
                "/root/.config/gcloud/application_default_credentials.json"
            )

        # Pass model configuration
        # Use container-specific model if configured, otherwise fall back to default
        env["LLM_MODEL"] = self.settings.container_model
        env["LLM_MAX_TOKENS"] = str(self.settings.llm_max_tokens)

        # Pass skill paths for agent (only if explicitly configured)
        if container_skill_paths:
            env["AGENT_SKILL_PATHS"] = container_skill_paths

        # Pass git configuration for commits
        env["GIT_USER_NAME"] = self.settings.git_user_name
        env["GIT_USER_EMAIL"] = self.settings.git_user_email

        # Pass Langfuse tracing credentials if enabled
        if self.settings.langfuse_enabled:
            env["LANGFUSE_PUBLIC_KEY"] = self.settings.langfuse_public_key
            env["LANGFUSE_SECRET_KEY"] = self.settings.langfuse_secret_key.get_secret_value()
            env["LANGFUSE_HOST"] = self.settings.langfuse_host
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

        Resolves skill directories via the resolver using settings.skills_dir as
        the base, with per-project overrides and fallback to skills/default/.

        Returns:
            Tuple of (mounts, container_paths) where:
            - mounts: List of (host_path, container_path) tuples
            - container_paths: Comma-separated paths for AGENT_SKILL_PATHS env var
        """
        skills_dir = Path.cwd() / self.settings.skills_dir.rstrip("/")
        host_paths = [
            Path(p.rstrip("/")) for p in resolve_skill_paths(ticket_key or "", skills_dir)
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
        if self.settings.use_vertex_ai:
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
        for key, value in self._build_env_vars(config, container_skill_paths).items():
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

        Returns:
            ContainerResult with execution status and logs.
        """
        config = config or self._default_config()

        # Create task file in .forge directory (excluded from commits)
        forge_dir = workspace_path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        task_file = forge_dir / "task.json"
        task_data = {
            "task_key": task_key or "UNKNOWN",
            "summary": task_summary,
            "description": task_description,
            "previous_task_keys": previous_task_keys or [],
        }
        task_file.write_text(json.dumps(task_data, indent=2))

        try:
            # Build container name and command
            container_name = self._build_container_name(ticket_key, repo_name)
            cmd = self._build_podman_command(
                workspace_path, task_file, config, container_name, ticket_key
            )

            logger.info(f"Starting container {container_name} for task: {task_summary}")
            logger.debug(f"Command: {' '.join(cmd)}")

            # Run container
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=config.timeout_seconds + 60,  # Extra buffer
                )
            except TimeoutError:
                logger.error("Container execution timed out")
                process.kill()
                await process.wait()
                return ContainerResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr="Container execution timed out",
                    error_message="Timeout exceeded",
                )
            except asyncio.CancelledError:
                logger.warning(f"Container execution cancelled, stopping {container_name}")
                # Stop the container via podman (more reliable than killing process)
                stop_process = await asyncio.create_subprocess_exec(
                    "podman",
                    "stop",
                    "-t",
                    "10",
                    container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(stop_process.wait(), timeout=15.0)
                except TimeoutError:
                    logger.warning(f"Container {container_name} didn't stop, killing")
                    kill_process = await asyncio.create_subprocess_exec(
                        "podman",
                        "kill",
                        container_name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await kill_process.wait()
                # Wait for the original process to finish
                await process.wait()
                raise  # Re-raise CancelledError

            exit_code = process.returncode or 0
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

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
                )
            elif exit_code == EXIT_TESTS_FAILED:
                return ContainerResult(
                    success=False,
                    exit_code=exit_code,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    tests_passed=False,
                    error_message="Tests failed after max retries",
                )
            else:
                return ContainerResult(
                    success=False,
                    exit_code=exit_code,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    error_message=f"Task failed with exit code {exit_code}",
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
