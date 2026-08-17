"""Podman container runtime driver."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from forge.sandbox.driver import ExecutionResult, ExecutionSpec, SandboxDriver

if TYPE_CHECKING:
    from forge.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "localhost/forge-dev:latest"


class PodmanDriver(SandboxDriver):
    """Sandbox driver using rootless Podman."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def is_available(self) -> bool:
        return shutil.which("podman") is not None

    def debug_hint(self, container_name: str) -> str | None:
        return (
            f"  Inspect logs:      podman logs {container_name}\n"
            f"  Enter filesystem:  podman export {container_name} | "
            f"tar -xC /tmp/{container_name}\n"
            f"  Remove when done:  podman rm {container_name}"
        )

    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        cmd = self._build_command(spec)

        logger.debug("Podman command: %s", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=spec.timeout_seconds + 60,
            )
        except TimeoutError:
            logger.error("Container execution timed out, stopping %s", spec.container_name)
            await self._stop_container(spec.container_name, process)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="Container execution timed out",
            )
        except asyncio.CancelledError:
            logger.warning("Container execution cancelled, stopping %s", spec.container_name)
            await self._stop_container(spec.container_name, process)
            raise

        return ExecutionResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    def _build_command(self, spec: ExecutionSpec) -> list[str]:
        """Translate ExecutionSpec into a podman run command."""
        cmd = ["podman", "run", "--name", spec.container_name]

        if spec.remove_after:
            cmd.append("--rm")

        for host_path, container_path, mode in spec.volume_mounts:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])

        cmd.extend(["--memory", spec.memory_limit])
        cmd.extend(["--cpus", spec.cpu_limit])
        cmd.extend(["--network", spec.network_mode])
        cmd.extend(["-w", "/workspace"])

        for key, value in spec.env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        cmd.extend(["--timeout", str(spec.timeout_seconds)])

        cmd.append(spec.image)

        cmd.extend(
            [
                "--task-file",
                "/task.json",
                "--max-retries",
                str(spec.max_retries),
            ]
        )

        if spec.skip_tests:
            cmd.append("--skip-tests")

        return cmd

    async def _stop_container(
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
                    "podman stop failed for %s (exit %s), killing",
                    container_name,
                    stop_process.returncode,
                )
                should_kill = True
        except TimeoutError:
            logger.warning("Container %s didn't stop, killing", container_name)
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
                logger.warning("podman kill for %s did not finish", container_name)

        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except TimeoutError:
            logger.warning("podman run process for %s did not exit, killing", container_name)
            process.kill()
            await process.wait()

    async def build_image(
        self,
        containerfile_path: Path | None = None,
        tag: str = DEFAULT_IMAGE,
    ) -> bool:
        if containerfile_path is None:
            project_root = Path(__file__).parent.parent.parent.parent
            containerfile_path = project_root / "containers" / "Containerfile"

        if not containerfile_path.exists():
            logger.error("Containerfile not found: %s", containerfile_path)
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

        logger.info("Building container image: %s", tag)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info("Successfully built image: %s", tag)
            return True
        else:
            logger.error("Failed to build image: %s", stderr.decode())
            return False

    async def image_exists(self, tag: str = DEFAULT_IMAGE) -> bool:
        cmd = ["podman", "image", "exists", tag]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        await process.wait()
        return process.returncode == 0

    async def pull_image(self, image: str) -> bool:
        cmd = ["podman", "pull", image]

        logger.info("Pulling image: %s", image)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _stdout, stderr = await process.communicate()

        if process.returncode == 0:
            logger.info("Successfully pulled image: %s", image)
            return True
        else:
            logger.error("Failed to pull image: %s", stderr.decode())
            return False
