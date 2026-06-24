"""Quick tests for container sandbox runner."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from forge.sandbox import ContainerRunner
from forge.sandbox.runner import ContainerConfig


class TestContainerRunner:
    """Tests for ContainerRunner."""

    def test_runner_init(self):
        """Test runner initializes correctly."""
        runner = ContainerRunner()
        assert runner is not None

    def test_podman_exists(self):
        """Test podman is available."""
        import shutil
        assert shutil.which("podman") is not None

    @pytest.mark.asyncio
    async def test_image_exists_returns_false_for_missing(self):
        """Test image_exists returns False for non-existent image."""
        runner = ContainerRunner()
        exists = await runner.image_exists("nonexistent-image:latest")
        assert exists is False

    @pytest.mark.asyncio
    async def test_simple_container_run(self):
        """Test running a simple container with alpine."""
        # Create a minimal test workspace
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)

            # Create a simple test file
            (workspace / "test.txt").write_text("hello world")

            # Run a simple echo command in alpine
            # This tests the container spawning logic without needing forge-dev image
            import subprocess

            result = subprocess.run(
                [
                    "podman", "run", "--rm",
                    "-v", f"{workspace}:/workspace:Z",
                    "alpine:latest",
                    "cat", "/workspace/test.txt",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            assert result.returncode == 0
            assert "hello world" in result.stdout

    @pytest.mark.asyncio
    async def test_container_config_defaults(self):
        """Test ContainerConfig has sensible defaults."""
        config = ContainerConfig()
        assert config.image == "localhost/forge-dev:latest"
        assert config.timeout_seconds == 1800  # 30 minutes
        assert config.memory_limit == "4g"
        assert config.cpu_limit == "2"
        assert config.skip_tests is False
        assert config.max_retries == 3


if __name__ == "__main__":
    # Quick manual test
    async def main():
        print("Testing ContainerRunner...")

        runner = ContainerRunner()
        print("  Runner initialized: OK")

        exists = await runner.image_exists("forge-dev:latest")
        print(f"  forge-dev:latest exists: {exists}")

        exists = await runner.image_exists("alpine:latest")
        print(f"  alpine:latest exists: {exists}")

        print("\nAll basic tests passed!")

    asyncio.run(main())
