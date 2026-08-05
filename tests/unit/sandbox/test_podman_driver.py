"""Tests for the Podman sandbox driver."""

from pathlib import Path

from forge.sandbox.driver import ExecutionSpec
from forge.sandbox.drivers.podman import PodmanDriver


def test_build_command_preserves_network_mode() -> None:
    spec = ExecutionSpec(
        container_name="forge-test-abc123",
        image="forge:test",
        workspace_path=Path("/workspace"),
        task_file=Path("/workspace/.forge/task.json"),
        env_vars={},
        memory_limit="4g",
        cpu_limit="2",
        network_mode="none",
        timeout_seconds=60,
        skip_tests=False,
        max_retries=3,
    )

    command = PodmanDriver()._build_command(spec)

    network_index = command.index("--network")
    assert command[network_index + 1] == "none"
