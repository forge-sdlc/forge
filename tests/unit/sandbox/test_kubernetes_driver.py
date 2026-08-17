"""Tests for the Kubernetes sandbox driver."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.sandbox.driver import ExecutionSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(**overrides: object) -> ExecutionSpec:
    defaults = {
        "container_name": "forge-TEST-abc123",
        "image": "localhost/forge-dev:latest",
        "workspace_path": Path("/mnt/workspaces/org/repo"),
        "task_file": Path("/mnt/workspaces/org/repo/.forge/task.json"),
        "env_vars": {"LLM_BACKEND": "anthropic", "LLM_MODEL": "test-model"},
        "memory_limit": "4g",
        "cpu_limit": "2",
        "network_mode": "slirp4netns",
        "timeout_seconds": 1800,
        "skip_tests": False,
        "max_retries": 3,
        "volume_mounts": [],
        "remove_after": True,
    }
    defaults.update(overrides)
    return ExecutionSpec(**defaults)


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.k8s_namespace = "forge"
    settings.k8s_workspace_pvc = "forge-workspaces"
    settings.k8s_workspace_base_path = "/mnt/workspaces"
    settings.k8s_image_pull_secrets = ""
    settings.k8s_service_account = ""
    settings.k8s_google_credentials_secret = ""
    settings.k8s_google_credentials_key = "forge-gcp-credentials.json"
    settings.k8s_google_credentials_mount_path = "/etc/forge-gcp-credentials.json"
    settings.k8s_run_as_user = None
    settings.k8s_fs_group = None
    return settings


def _make_driver(settings: MagicMock | None = None):
    from forge.sandbox.drivers.kubernetes import KubernetesDriver

    return KubernetesDriver(settings or _make_settings())


class _Stub:
    """Stand-in for kubernetes.client model objects."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _build_fake_k8s_modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    """Build fake kubernetes, kubernetes.client, kubernetes.config modules."""
    k8s_top = ModuleType("kubernetes")
    k8s_client = ModuleType("kubernetes.client")
    k8s_rest = ModuleType("kubernetes.client.rest")
    k8s_config = ModuleType("kubernetes.config")

    for name in (
        "V1EnvVar",
        "V1VolumeMount",
        "V1Volume",
        "V1PersistentVolumeClaimVolumeSource",
        "V1ResourceRequirements",
        "V1SecurityContext",
        "V1PodSecurityContext",
        "V1Capabilities",
        "V1SeccompProfile",
        "V1Container",
        "V1PodSpec",
        "V1ObjectMeta",
        "V1PodTemplateSpec",
        "V1JobSpec",
        "V1Job",
        "V1DeleteOptions",
        "V1LocalObjectReference",
        "V1SecretVolumeSource",
    ):
        setattr(k8s_client, name, type(name, (_Stub,), {}))

    k8s_rest.ApiException = type("ApiException", (Exception,), {})  # type: ignore[attr-defined]

    k8s_top.client = k8s_client  # type: ignore[attr-defined]
    k8s_top.config = k8s_config  # type: ignore[attr-defined]
    k8s_config.load_config = lambda: None  # type: ignore[attr-defined]

    return k8s_top, k8s_client, k8s_config, k8s_rest


@pytest.fixture()
def fake_k8s():
    """Patch sys.modules so `from kubernetes import client` resolves to stubs."""
    k8s_top, k8s_client, k8s_config, k8s_rest = _build_fake_k8s_modules()
    saved = {
        k: sys.modules.get(k)
        for k in (
            "kubernetes",
            "kubernetes.client",
            "kubernetes.client.rest",
            "kubernetes.config",
        )
    }
    sys.modules["kubernetes"] = k8s_top
    sys.modules["kubernetes.client"] = k8s_client
    sys.modules["kubernetes.client.rest"] = k8s_rest
    sys.modules["kubernetes.config"] = k8s_config
    run_in_executor = AsyncMock(side_effect=lambda _executor, func: func())
    with patch.object(asyncio.BaseEventLoop, "run_in_executor", run_in_executor):
        yield k8s_client, k8s_config
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkspaceSubpath:
    def test_relative_path_from_base(self) -> None:
        driver = _make_driver()
        assert driver._workspace_subpath(Path("/mnt/workspaces/org/repo")) == "org/repo"

    def test_driver_rejects_missing_base_path(self) -> None:
        settings = _make_settings()
        settings.k8s_workspace_base_path = ""
        with pytest.raises(ValueError, match="K8S_WORKSPACE_BASE_PATH"):
            _make_driver(settings)

    def test_driver_rejects_missing_pvc(self) -> None:
        settings = _make_settings()
        settings.k8s_workspace_pvc = ""
        with pytest.raises(ValueError, match="K8S_WORKSPACE_PVC"):
            _make_driver(settings)

    def test_rejects_workspace_outside_base(self) -> None:
        driver = _make_driver()
        with pytest.raises(ValueError, match="outside K8S_WORKSPACE_BASE_PATH"):
            driver._workspace_subpath(Path("/other/location"))


class TestDebugHint:
    def test_debug_hint_references_job_and_namespace(self) -> None:
        driver = _make_driver()
        hint = driver.debug_hint("forge-TEST-abc123")

        assert hint is not None
        assert "kubectl logs -n forge job/forge-test-abc123" in hint
        assert "kubectl delete -n forge job/forge-test-abc123" in hint


class TestBuildJobManifest:
    def test_basic_manifest_structure(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())

        assert job.metadata.name == "forge-test-abc123"
        assert job.metadata.namespace == "forge"
        assert job.spec.backoff_limit == 0
        assert job.spec.active_deadline_seconds == 1800

        container = job.spec.template.spec.containers[0]
        assert container.name == "forge-task"
        assert container.image == "localhost/forge-dev:latest"
        assert container.working_dir == "/workspace"

    def test_env_vars_passed(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(env_vars={"KEY1": "val1", "KEY2": "val2"}))
        container = job.spec.template.spec.containers[0]
        env_names = {e.name for e in container.env}
        assert {"KEY1", "KEY2"} <= env_names

    def test_resource_limits(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(memory_limit="8g", cpu_limit="4"))
        res = job.spec.template.spec.containers[0].resources
        assert res.requests["memory"] == "8Gi"
        assert res.requests["cpu"] == "4"
        assert res.limits["memory"] == "8Gi"
        assert res.limits["cpu"] == "4"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [("512m", "512Mi"), ("1.5g", "1.5Gi"), ("4Gi", "4Gi"), ("500M", "500M")],
    )
    def test_memory_quantity_normalization(self, fake_k8s, configured: str, expected: str) -> None:
        del fake_k8s
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(memory_limit=configured))
        resources = job.spec.template.spec.containers[0].resources
        assert resources.requests["memory"] == expected
        assert resources.limits["memory"] == expected

    def test_skip_tests_flag(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(skip_tests=True))
        assert "--skip-tests" in job.spec.template.spec.containers[0].args

    def test_workspace_pvc_mounted(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())

        vol = job.spec.template.spec.volumes[0]
        assert vol.name == "workspace"
        assert vol.persistent_volume_claim.claim_name == "forge-workspaces"

        mount = job.spec.template.spec.containers[0].volume_mounts[0]
        assert mount.mount_path == "/workspace"
        assert mount.sub_path == "org/repo"

    def test_workspace_file_mount_uses_pvc_subpath(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        spec = _make_spec(
            volume_mounts=[
                (
                    Path("/mnt/workspaces/org/repo/.forge/task.json"),
                    "/task.json",
                    "ro,Z",
                )
            ]
        )

        job = driver._build_job_manifest(spec)

        mount = job.spec.template.spec.containers[0].volume_mounts[1]
        assert mount.mount_path == "/task.json"
        assert mount.sub_path == "org/repo/.forge/task.json"
        assert mount.read_only is True

    def test_google_credentials_secret_mounted(self, fake_k8s) -> None:  # noqa: ARG002
        settings = _make_settings()
        settings.k8s_google_credentials_secret = "google-adc"
        driver = _make_driver(settings)

        job = driver._build_job_manifest(
            _make_spec(env_vars={"GOOGLE_APPLICATION_CREDENTIALS": "/worker/adc.json"})
        )

        pod_spec = job.spec.template.spec
        credential_volume = next(v for v in pod_spec.volumes if v.name == "google-credentials")
        credential_mount = next(
            m for m in pod_spec.containers[0].volume_mounts if m.name == "google-credentials"
        )
        credential_env = next(
            e for e in pod_spec.containers[0].env if e.name == "GOOGLE_APPLICATION_CREDENTIALS"
        )

        assert credential_volume.secret.secret_name == "google-adc"
        assert credential_mount.mount_path == "/etc/forge-gcp-credentials.json"
        assert credential_mount.sub_path == "forge-gcp-credentials.json"
        assert credential_mount.read_only is True
        assert credential_env.value == "/etc/forge-gcp-credentials.json"

    def test_google_credentials_omitted_when_unconfigured(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())

        pod_spec = job.spec.template.spec
        assert all(v.name != "google-credentials" for v in pod_spec.volumes)
        assert all(m.name != "google-credentials" for m in pod_spec.containers[0].volume_mounts)

    def test_job_name_is_dns_safe_and_preserves_unique_suffix(self) -> None:
        driver = _make_driver()

        name = driver._job_name("forge-FEATURE_WITH.INVALID*CHARS-abc123")
        long_name = driver._job_name(f"forge-{'A' * 100}-abc123")

        assert name == "forge-feature-with-invalid-chars-abc123"
        assert len(long_name) <= 63
        assert long_name.endswith("-abc123")


class TestExternalMountStaging:
    def test_copies_external_file_and_directory_into_workspace(self, tmp_path) -> None:
        driver = _make_driver()
        workspace = tmp_path / "workspaces" / "org" / "repo"
        workspace.mkdir(parents=True)
        external_file = tmp_path / "credentials.json"
        external_file.write_text("secret")
        external_dir = tmp_path / "skills"
        external_dir.mkdir()
        (external_dir / "SKILL.md").write_text("instructions")
        spec = _make_spec(
            workspace_path=workspace,
            task_file=workspace / ".forge" / "task.json",
            volume_mounts=[
                (external_file, "/credentials.json", "ro"),
                (external_dir, "/skills/example", "ro"),
            ],
        )

        prepared, staging_dir = driver._stage_external_mounts(spec)

        assert staging_dir is not None
        assert prepared.volume_mounts[0][0].read_text() == "secret"
        assert (prepared.volume_mounts[1][0] / "SKILL.md").read_text() == "instructions"
        assert all(source.is_relative_to(workspace) for source, _, _ in prepared.volume_mounts)

        driver._remove_staging_dir(staging_dir)
        assert not staging_dir.exists()

    def test_partial_staging_is_removed_when_copy_fails(self, tmp_path) -> None:
        driver = _make_driver()
        workspace = tmp_path / "workspaces" / "org" / "repo"
        workspace.mkdir(parents=True)
        external_file = tmp_path / "credentials.json"
        external_file.write_text("secret")
        spec = _make_spec(
            workspace_path=workspace,
            task_file=workspace / ".forge" / "task.json",
            volume_mounts=[(external_file, "/credentials.json", "ro")],
        )

        with (
            patch("forge.sandbox.drivers.kubernetes.shutil.copy2", side_effect=OSError("full")),
            pytest.raises(OSError, match="full"),
        ):
            driver._stage_external_mounts(spec)

        staging_dir = workspace / ".forge" / "k8s-mounts" / "forge-test-abc123"
        assert not staging_dir.exists()

    def test_does_not_stage_google_credentials_when_secret_is_configured(self, tmp_path) -> None:
        settings = _make_settings()
        settings.k8s_google_credentials_secret = "google-adc"
        driver = _make_driver(settings)
        workspace = tmp_path / "workspaces" / "org" / "repo"
        workspace.mkdir(parents=True)
        credentials = tmp_path / "credentials.json"
        credentials.write_text("secret")
        spec = _make_spec(
            workspace_path=workspace,
            task_file=workspace / ".forge" / "task.json",
            volume_mounts=[
                (
                    credentials,
                    "/root/.config/gcloud/application_default_credentials.json",
                    "ro",
                )
            ],
        )

        prepared, staging_dir = driver._stage_external_mounts(spec)

        assert prepared.volume_mounts == []
        assert staging_dir is None
        assert not (workspace / ".forge").exists()

    def test_image_pull_secrets(self, fake_k8s) -> None:  # noqa: ARG002
        settings = _make_settings()
        settings.k8s_image_pull_secrets = "secret1,secret2"
        driver = _make_driver(settings)
        job = driver._build_job_manifest(_make_spec())
        names = [s.name for s in job.spec.template.spec.image_pull_secrets]
        assert names == ["secret1", "secret2"]

    def test_service_account(self, fake_k8s) -> None:  # noqa: ARG002
        settings = _make_settings()
        settings.k8s_service_account = "forge-runner"
        driver = _make_driver(settings)
        job = driver._build_job_manifest(_make_spec())
        assert job.spec.template.spec.service_account_name == "forge-runner"

    def test_no_service_account_when_empty(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())
        assert job.spec.template.spec.service_account_name is None

    def test_managed_by_label(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())
        assert job.metadata.labels["app.kubernetes.io/managed-by"] == "forge"
        assert job.spec.template.metadata.labels["app.kubernetes.io/managed-by"] == "forge"

    def test_sandbox_isolation(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(network_mode="none"))

        pod_spec = job.spec.template.spec
        security = pod_spec.containers[0].security_context
        assert pod_spec.automount_service_account_token is False
        assert pod_spec.security_context.run_as_non_root is True
        assert pod_spec.security_context.seccomp_profile.type == "RuntimeDefault"
        assert security.allow_privilege_escalation is False
        assert security.run_as_non_root is True
        assert security.capabilities.drop == ["ALL"]
        assert security.seccomp_profile.type == "RuntimeDefault"
        assert job.spec.template.metadata.labels["forge.sdlc/component"] == "sandbox"
        assert job.spec.template.metadata.labels["forge.sdlc/network-access"] == "disabled"

    def test_networked_sandbox_gets_external_access_label(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec(network_mode="slirp4netns"))

        assert job.spec.template.metadata.labels["forge.sdlc/network-access"] == "external"

    def test_run_as_user_and_fs_group_omitted_by_default(self, fake_k8s) -> None:  # noqa: ARG002
        driver = _make_driver()
        job = driver._build_job_manifest(_make_spec())

        security = job.spec.template.spec.security_context
        assert getattr(security, "run_as_user", None) is None
        assert getattr(security, "fs_group", None) is None

    def test_run_as_user_and_fs_group_applied_when_configured(self, fake_k8s) -> None:  # noqa: ARG002
        settings = _make_settings()
        settings.k8s_run_as_user = 1000
        settings.k8s_fs_group = 1000
        driver = _make_driver(settings)

        job = driver._build_job_manifest(_make_spec())

        security = job.spec.template.spec.security_context
        assert security.run_as_user == 1000
        assert security.fs_group == 1000
        assert security.fs_group_change_policy == "OnRootMismatch"


class TestExecute:
    @pytest.mark.asyncio
    async def test_successful_execution(self, fake_k8s) -> None:
        k8s_client_mod, _ = fake_k8s
        driver = _make_driver()

        mock_batch = MagicMock()
        mock_core = MagicMock()

        job_status = _Stub(succeeded=1, failed=None, conditions=[])
        mock_batch.create_namespaced_job = MagicMock()
        mock_batch.read_namespaced_job_status = MagicMock(return_value=_Stub(status=job_status))
        mock_batch.delete_namespaced_job = MagicMock()

        mock_pod = _Stub(metadata=_Stub(name="forge-TEST-abc123-pod"))
        mock_core.list_namespaced_pod = MagicMock(return_value=_Stub(items=[mock_pod]))
        mock_core.read_namespaced_pod_log = MagicMock(return_value="task output")

        k8s_client_mod.BatchV1Api = MagicMock(return_value=mock_batch)
        k8s_client_mod.CoreV1Api = MagicMock(return_value=mock_core)

        result = await driver.execute(_make_spec())

        assert result.exit_code == 0
        assert result.stdout == "task output"
        mock_batch.delete_namespaced_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_execution(self, fake_k8s) -> None:
        k8s_client_mod, _ = fake_k8s
        driver = _make_driver()

        mock_batch = MagicMock()
        mock_core = MagicMock()

        failed_cond = _Stub(type="Failed", status="True", reason="BackoffLimitExceeded")
        job_status = _Stub(succeeded=None, failed=1, conditions=[failed_cond])
        mock_batch.create_namespaced_job = MagicMock()
        mock_batch.read_namespaced_job_status = MagicMock(return_value=_Stub(status=job_status))
        mock_batch.delete_namespaced_job = MagicMock()

        terminated = _Stub(exit_code=2)
        container_status = _Stub(name="forge-task", state=_Stub(terminated=terminated))
        mock_pod = _Stub(
            metadata=_Stub(name="forge-TEST-abc123-pod"),
            status=_Stub(container_statuses=[container_status]),
        )
        mock_core.list_namespaced_pod = MagicMock(return_value=_Stub(items=[mock_pod]))
        mock_core.read_namespaced_pod_log = MagicMock(return_value="tests failed")

        k8s_client_mod.BatchV1Api = MagicMock(return_value=mock_batch)
        k8s_client_mod.CoreV1Api = MagicMock(return_value=mock_core)

        result = await driver.execute(_make_spec())

        assert result.exit_code == 2
        assert result.stdout == "tests failed"

    @pytest.mark.asyncio
    async def test_no_cleanup_when_remove_after_false(self, fake_k8s) -> None:
        k8s_client_mod, _ = fake_k8s
        driver = _make_driver()

        mock_batch = MagicMock()
        mock_core = MagicMock()

        job_status = _Stub(succeeded=1, failed=None, conditions=[])
        mock_batch.create_namespaced_job = MagicMock()
        mock_batch.read_namespaced_job_status = MagicMock(return_value=_Stub(status=job_status))

        mock_core.list_namespaced_pod = MagicMock(return_value=_Stub(items=[]))

        k8s_client_mod.BatchV1Api = MagicMock(return_value=mock_batch)
        k8s_client_mod.CoreV1Api = MagicMock(return_value=mock_core)

        result = await driver.execute(_make_spec(remove_after=False))

        assert result.exit_code == 0
        mock_batch.delete_namespaced_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_api_error_is_returned_as_stderr(self, fake_k8s) -> None:
        del fake_k8s
        from kubernetes.client.rest import ApiException

        driver = _make_driver()
        mock_core = MagicMock()
        mock_core.list_namespaced_pod.side_effect = ApiException("logs unavailable")

        stdout, stderr = await driver._collect_logs(mock_core, "forge-test-abc123")

        assert stdout == ""
        assert "logs unavailable" in stderr

    @pytest.mark.asyncio
    async def test_delete_api_error_is_handled(self, fake_k8s) -> None:
        del fake_k8s
        from kubernetes.client.rest import ApiException

        driver = _make_driver()
        mock_batch = MagicMock()
        mock_batch.delete_namespaced_job.side_effect = ApiException("already deleted")

        await driver._delete_job(mock_batch, "forge-test-abc123")


class TestDriverSelection:
    def test_podman_driver_selected_by_default(self) -> None:
        from forge.sandbox.drivers.podman import PodmanDriver

        settings = MagicMock()
        settings.sandbox_driver = "podman"

        with patch("shutil.which", return_value="/usr/bin/podman"):
            from forge.sandbox.drivers import create_driver

            driver = create_driver(settings)
            assert isinstance(driver, PodmanDriver)

    def test_unknown_driver_raises(self) -> None:
        settings = MagicMock()
        settings.sandbox_driver = "docker"

        from forge.sandbox.drivers import create_driver

        with pytest.raises(ValueError, match="Unknown sandbox driver"):
            create_driver(settings)
