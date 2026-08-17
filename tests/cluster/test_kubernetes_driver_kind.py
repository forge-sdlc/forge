"""Real-cluster gate for the Kubernetes sandbox driver using Kind."""

import json
import os
from pathlib import Path

import pytest

from forge.config import Settings
from forge.sandbox.driver import ExecutionSpec
from forge.sandbox.drivers.kubernetes import KubernetesDriver

pytestmark = [
    pytest.mark.cluster,
    pytest.mark.skipif(
        "KIND_WORKSPACE_BASE" not in os.environ,
        reason="requires the Kind cluster gate workspace",
    ),
]


def _driver() -> KubernetesDriver:
    workspace_base = os.environ["KIND_WORKSPACE_BASE"]
    settings = Settings(
        forge_sandbox_driver="kubernetes",
        k8s_namespace="forge",
        k8s_workspace_pvc="forge-workspaces",
        k8s_workspace_base_path=workspace_base,
        # The host-side test process represents the worker. Use its ownership
        # for the sandbox too, matching the chart's shared-UID contract.
        k8s_run_as_user=os.getuid(),
        k8s_fs_group=os.getgid(),
        k8s_service_account="default",
    )
    driver = KubernetesDriver(settings)
    driver._poll_interval = 0.2
    return driver


def _spec(workspace: Path, *, exit_code: int) -> ExecutionSpec:
    task_file = workspace / ".forge" / "task.json"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(json.dumps({"task_key": f"KIND-{exit_code}"}), encoding="utf-8")
    return ExecutionSpec(
        container_name=f"forge-kind-exit-{exit_code}",
        image="forge-sandbox-fixture:ci",
        workspace_path=workspace,
        task_file=task_file,
        env_vars={"FIXTURE_EXIT_CODE": str(exit_code)},
        memory_limit="128Mi",
        cpu_limit="250m",
        network_mode="none",
        timeout_seconds=60,
        skip_tests=True,
        max_retries=0,
        volume_mounts=[(workspace, "/workspace", "")],
        remove_after=True,
    )


@pytest.mark.asyncio
async def test_kind_job_writes_to_shared_workspace() -> None:
    workspace = Path(os.environ["KIND_WORKSPACE_BASE"]) / "success"
    workspace.mkdir(parents=True, exist_ok=True)

    result = await _driver().execute(_spec(workspace, exit_code=0))

    assert result.exit_code == 0
    assert "fixture task=KIND-0 exit_code=0" in result.stdout
    assert (workspace / "kind-result.txt").read_text() == "KIND-0:KUBERNETES_DRIVER_OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [2, 3])
async def test_kind_job_preserves_entrypoint_exit_code(exit_code: int) -> None:
    workspace = Path(os.environ["KIND_WORKSPACE_BASE"]) / f"exit-{exit_code}"
    workspace.mkdir(parents=True, exist_ok=True)

    result = await _driver().execute(_spec(workspace, exit_code=exit_code))

    assert result.exit_code == exit_code
    assert f"exit_code={exit_code}" in result.stdout
