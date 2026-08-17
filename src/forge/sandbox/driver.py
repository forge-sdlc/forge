"""Abstract sandbox driver interface for container runtime backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExecutionSpec:
    """Runtime-agnostic specification for a container execution.

    Built by the orchestration layer (ContainerRunner), consumed by a
    SandboxDriver implementation.
    """

    container_name: str
    image: str
    workspace_path: Path
    task_file: Path
    env_vars: dict[str, str]
    memory_limit: str
    cpu_limit: str
    network_mode: str
    timeout_seconds: int
    skip_tests: bool
    max_retries: int
    volume_mounts: list[tuple[Path, str, str]] = field(default_factory=list)
    remove_after: bool = True


@dataclass
class ExecutionResult:
    """Raw result from driver execution, before interpretation."""

    exit_code: int
    stdout: str
    stderr: str


class SandboxDriver(ABC):
    """Abstract interface for container runtime backends.

    Implementations handle the runtime-specific details of creating and
    running containers or pods. The orchestration layer (ContainerRunner)
    builds an ExecutionSpec and delegates to the driver.
    """

    @abstractmethod
    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        """Execute a container/pod with the given specification.

        Must handle starting the container/pod, waiting for completion
        (with timeout), capturing stdout/stderr, and cleanup on timeout
        or cancellation.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this driver's runtime is available."""
        ...

    def debug_hint(self, container_name: str) -> str | None:  # noqa: ARG002
        """Return runtime-specific instructions for inspecting a kept container.

        Only meaningful when the container/pod is preserved after execution
        (e.g. FORGE_CONTAINER_KEEP=true). Returns None when the driver has no
        additional guidance to offer.
        """
        return None

    async def build_image(self, containerfile_path: Path | None = None, tag: str = "") -> bool:
        """Build a container image. Optional — not all drivers support this."""
        raise NotImplementedError(f"{type(self).__name__} does not support local image building")

    async def image_exists(self, tag: str) -> bool:  # noqa: ARG002
        """Check if an image exists locally."""
        return False

    async def pull_image(self, image: str) -> bool:
        """Pull a container image."""
        raise NotImplementedError(f"{type(self).__name__} does not support image pulling")
