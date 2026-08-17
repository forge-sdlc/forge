"""Kubernetes Job-based sandbox driver."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.sandbox.driver import ExecutionResult, ExecutionSpec, SandboxDriver

if TYPE_CHECKING:
    from forge.config import Settings

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 5.0
_COMPLETION_WAIT_BUFFER = 120
_LEGACY_GOOGLE_CREDENTIALS_PATH = "/root/.config/gcloud/application_default_credentials.json"
_PODMAN_MEMORY_UNITS = {
    "k": "Ki",
    "m": "Mi",
    "g": "Gi",
    "t": "Ti",
    "p": "Pi",
    "e": "Ei",
}


def _normalize_memory_quantity(value: str) -> str:
    """Translate Podman-style memory suffixes into Kubernetes quantities."""
    match = re.fullmatch(r"([+-]?[0-9]+(?:\.[0-9]+)?)([kmgtpe])", value)
    if not match:
        return value
    number, unit = match.groups()
    return f"{number}{_PODMAN_MEMORY_UNITS[unit]}"


class KubernetesDriver(SandboxDriver):
    """Sandbox driver that runs tasks as Kubernetes Jobs.

    Workspaces are shared between the Forge worker and sandbox pods via
    a PVC.  The worker writes task.json to the workspace before the Job
    starts; the pod mounts the same PVC at /workspace.  Review-cycle
    polling works unchanged because both sides see the same filesystem.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._namespace = settings.k8s_namespace
        self._workspace_pvc = settings.k8s_workspace_pvc.strip()
        self._workspace_base_path = settings.k8s_workspace_base_path.strip()
        if not self._workspace_pvc:
            raise ValueError("K8S_WORKSPACE_PVC is required for the Kubernetes driver")
        if not self._workspace_base_path:
            raise ValueError("K8S_WORKSPACE_BASE_PATH is required for the Kubernetes driver")
        self._image_pull_secrets = [
            s.strip() for s in settings.k8s_image_pull_secrets.split(",") if s.strip()
        ]
        self._service_account = settings.k8s_service_account
        self._google_credentials_secret = settings.k8s_google_credentials_secret
        self._google_credentials_key = settings.k8s_google_credentials_key
        self._google_credentials_mount_path = settings.k8s_google_credentials_mount_path
        self._run_as_user = settings.k8s_run_as_user
        self._fs_group = settings.k8s_fs_group
        self._poll_interval = _DEFAULT_POLL_INTERVAL

    # ------------------------------------------------------------------
    # SandboxDriver interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            import kubernetes  # noqa: F401

            return True
        except ImportError:
            return False

    def debug_hint(self, container_name: str) -> str | None:
        job_name = self._job_name(container_name)
        namespace = self._namespace
        return (
            f"  Inspect logs:      kubectl logs -n {namespace} job/{job_name}\n"
            f"  Describe job:      kubectl describe -n {namespace} job/{job_name}\n"
            f"  Remove when done:  kubectl delete -n {namespace} job/{job_name}"
        )

    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        k8s_config.load_config()
        batch_api = k8s_client.BatchV1Api()
        core_api = k8s_client.CoreV1Api()

        job_name = self._job_name(spec.container_name)
        staging_dir: Path | None = None
        job_created = False
        job_deleted = False
        try:
            prepared_spec, staging_dir = await asyncio.to_thread(self._stage_external_mounts, spec)
            job_manifest = self._build_job_manifest(prepared_spec, job_name=job_name)
            logger.info("Creating K8s Job %s in namespace %s", job_name, self._namespace)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: batch_api.create_namespaced_job(
                    namespace=self._namespace,
                    body=job_manifest,
                ),
            )
            job_created = True
            exit_code = await self._wait_for_completion(batch_api, job_name, spec.timeout_seconds)
            if exit_code not in (0, -1):
                exit_code = await self._read_pod_exit_code(core_api, job_name, default=exit_code)
            stdout, stderr = await self._collect_logs(core_api, job_name)

            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        except asyncio.CancelledError:
            logger.warning("Execution cancelled, deleting Job %s", job_name)
            if job_created:
                await self._delete_job(batch_api, job_name)
                job_deleted = True
            raise
        finally:
            if spec.remove_after and job_created and not job_deleted:
                await self._delete_job(batch_api, job_name)
            if spec.remove_after or not job_created:
                self._remove_staging_dir(staging_dir)

    # ------------------------------------------------------------------
    # Job manifest construction
    # ------------------------------------------------------------------

    def _build_job_manifest(
        self, spec: ExecutionSpec, *, job_name: str | None = None
    ) -> dict[str, Any]:
        from kubernetes import client as k8s_client

        env_values = dict(spec.env_vars)
        if self._google_credentials_secret:
            env_values["GOOGLE_APPLICATION_CREDENTIALS"] = self._google_credentials_mount_path
        env_vars = [k8s_client.V1EnvVar(name=k, value=v) for k, v in env_values.items()]

        volume_mounts = [
            k8s_client.V1VolumeMount(
                name="workspace",
                mount_path="/workspace",
                sub_path=self._workspace_subpath(spec.workspace_path),
            ),
        ]

        workspace_subpath = Path(self._workspace_subpath(spec.workspace_path))
        for host_path, container_path, mode in spec.volume_mounts:
            if container_path == "/workspace":
                continue
            try:
                relative_path = host_path.relative_to(spec.workspace_path)
            except ValueError as exc:
                raise ValueError(
                    f"Kubernetes mount source must be in the shared workspace: {host_path}"
                ) from exc
            volume_mounts.append(
                k8s_client.V1VolumeMount(
                    name="workspace",
                    mount_path=container_path,
                    sub_path=str(workspace_subpath / relative_path),
                    read_only="ro" in mode.split(","),
                )
            )

        volumes = [
            k8s_client.V1Volume(
                name="workspace",
                persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=self._workspace_pvc,
                ),
            ),
        ]

        if self._google_credentials_secret:
            volume_mounts.append(
                k8s_client.V1VolumeMount(
                    name="google-credentials",
                    mount_path=self._google_credentials_mount_path,
                    sub_path=self._google_credentials_key,
                    read_only=True,
                )
            )
            volumes.append(
                k8s_client.V1Volume(
                    name="google-credentials",
                    secret=k8s_client.V1SecretVolumeSource(
                        secret_name=self._google_credentials_secret,
                    ),
                )
            )

        container_args = [
            "--task-file",
            "/workspace/.forge/task.json",
            "--max-retries",
            str(spec.max_retries),
        ]
        if spec.skip_tests:
            container_args.append("--skip-tests")

        memory_limit = _normalize_memory_quantity(spec.memory_limit)
        resources = k8s_client.V1ResourceRequirements(
            requests={"memory": memory_limit, "cpu": spec.cpu_limit},
            limits={"memory": memory_limit, "cpu": spec.cpu_limit},
        )

        container = k8s_client.V1Container(
            name="forge-task",
            image=spec.image,
            args=container_args,
            env=env_vars,
            volume_mounts=volume_mounts,
            resources=resources,
            working_dir="/workspace",
            security_context=k8s_client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=k8s_client.V1Capabilities(drop=["ALL"]),
                run_as_non_root=True,
                seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
            ),
        )

        network_access = "disabled" if spec.network_mode == "none" else "external"
        pod_labels = {
            "app.kubernetes.io/managed-by": "forge",
            "forge.sdlc/component": "sandbox",
            "forge.sdlc/network-access": network_access,
        }

        # UID and fsGroup are left unset by default so OpenShift's SCC can assign
        # them from the namespace range. On vanilla Kubernetes they must be set
        # explicitly, otherwise runAsNonRoot cannot be satisfied and the pod
        # cannot write the worker-owned files on the shared workspace PVC.
        pod_security_context = k8s_client.V1PodSecurityContext(
            run_as_non_root=True,
            seccomp_profile=k8s_client.V1SeccompProfile(type="RuntimeDefault"),
        )
        if self._run_as_user is not None:
            pod_security_context.run_as_user = self._run_as_user
        if self._fs_group is not None:
            pod_security_context.fs_group = self._fs_group
            pod_security_context.fs_group_change_policy = "OnRootMismatch"

        pod_spec = k8s_client.V1PodSpec(
            containers=[container],
            volumes=volumes,
            restart_policy="Never",
            service_account_name=self._service_account or None,
            automount_service_account_token=False,
            security_context=pod_security_context,
        )

        if self._image_pull_secrets:
            pod_spec.image_pull_secrets = [
                k8s_client.V1LocalObjectReference(name=s) for s in self._image_pull_secrets
            ]

        template = k8s_client.V1PodTemplateSpec(
            metadata=k8s_client.V1ObjectMeta(labels=pod_labels),
            spec=pod_spec,
        )

        job_spec = k8s_client.V1JobSpec(
            template=template,
            backoff_limit=0,
            active_deadline_seconds=spec.timeout_seconds,
        )

        job = k8s_client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s_client.V1ObjectMeta(
                name=job_name or self._job_name(spec.container_name),
                namespace=self._namespace,
                labels={"app.kubernetes.io/managed-by": "forge"},
            ),
            spec=job_spec,
        )

        return job

    @staticmethod
    def _job_name(container_name: str) -> str:
        """Convert a runner identifier to a valid Kubernetes DNS label."""
        name = re.sub(r"[^a-z0-9-]+", "-", container_name.lower()).strip("-")
        name = re.sub(r"-+", "-", name)
        if not name:
            raise ValueError("Container name does not contain any DNS-label characters")
        if len(name) <= 63:
            return name
        prefix, separator, suffix = name.rpartition("-")
        if separator and suffix:
            return f"{prefix[: 62 - len(suffix)].rstrip('-')}-{suffix}"
        return name[:63].rstrip("-")

    def _stage_external_mounts(self, spec: ExecutionSpec) -> tuple[ExecutionSpec, Path | None]:
        """Copy worker-local mounts into the workspace shared through the PVC."""
        expected_staging_dir = (
            spec.workspace_path / ".forge" / "k8s-mounts" / self._job_name(spec.container_name)
        )
        try:
            return self._stage_external_mounts_unchecked(spec)
        except Exception:
            self._remove_staging_dir(expected_staging_dir)
            raise

    def _stage_external_mounts_unchecked(
        self, spec: ExecutionSpec
    ) -> tuple[ExecutionSpec, Path | None]:
        staged_mounts: list[tuple[Path, str, str]] = []
        staging_dir: Path | None = None

        for index, (host_path, container_path, mode) in enumerate(spec.volume_mounts):
            if (
                self._google_credentials_secret
                and container_path == _LEGACY_GOOGLE_CREDENTIALS_PATH
            ):
                # The Job mounts the credential directly from a Secret. Copying the
                # worker credential into the shared workspace would be redundant and
                # would leave sensitive material on the PVC.
                continue
            try:
                host_path.relative_to(spec.workspace_path)
                staged_mounts.append((host_path, container_path, mode))
                continue
            except ValueError:
                pass

            if staging_dir is None:
                staging_dir = (
                    spec.workspace_path
                    / ".forge"
                    / "k8s-mounts"
                    / self._job_name(spec.container_name)
                )
                staging_dir.mkdir(parents=True, exist_ok=False)

            staged_path = staging_dir / f"mount-{index}"
            if host_path.is_dir():
                shutil.copytree(host_path, staged_path)
            else:
                shutil.copy2(host_path, staged_path)
            staged_mounts.append((staged_path, container_path, mode))

        return replace(spec, volume_mounts=staged_mounts), staging_dir

    @staticmethod
    def _remove_staging_dir(staging_dir: Path | None) -> None:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _workspace_subpath(self, workspace_path: Path) -> str:
        """Derive PVC sub-path from the local workspace path.

        If `k8s_workspace_base_path` is `/mnt/workspaces` and the
        workspace is at `/mnt/workspaces/org/repo`, the sub-path is
        `org/repo`.
        """
        try:
            return str(workspace_path.relative_to(self._workspace_base_path))
        except ValueError as exc:
            raise ValueError(
                f"Workspace {workspace_path} is outside K8S_WORKSPACE_BASE_PATH "
                f"{self._workspace_base_path}"
            ) from exc

    async def _wait_for_completion(
        self,
        batch_api: Any,
        job_name: str,
        timeout_seconds: int,
    ) -> int:
        """Poll Job status until completion or timeout."""
        from kubernetes import client as k8s_client

        deadline = asyncio.get_event_loop().time() + timeout_seconds + _COMPLETION_WAIT_BUFFER
        loop = asyncio.get_running_loop()

        while asyncio.get_event_loop().time() < deadline:
            job: k8s_client.V1Job = await loop.run_in_executor(
                None,
                lambda: batch_api.read_namespaced_job_status(
                    name=job_name,
                    namespace=self._namespace,
                ),
            )

            status = job.status
            if status.succeeded and status.succeeded > 0:
                return 0
            if status.failed and status.failed > 0:
                return self._extract_exit_code(job)

            conditions = status.conditions or []
            for cond in conditions:
                if cond.type == "Failed" and cond.status == "True":
                    if cond.reason == "DeadlineExceeded":
                        logger.error("Job %s exceeded deadline", job_name)
                        return -1
                    return self._extract_exit_code(job)

            await asyncio.sleep(self._poll_interval)

        logger.error("Timed out waiting for Job %s", job_name)
        return -1

    @staticmethod
    def _extract_exit_code(job: Any) -> int:
        """Best-effort extraction of the container exit code from a Job."""
        with contextlib.suppress(AttributeError):
            conditions = job.status.conditions or []
            for cond in conditions:
                if (
                    cond.type == "Failed"
                    and cond.status == "True"
                    and cond.reason == "DeadlineExceeded"
                ):
                    return -1
        return 1

    async def _collect_logs(
        self,
        core_api: Any,
        job_name: str,
    ) -> tuple[str, str]:
        """Retrieve stdout from the first pod of the Job."""
        from kubernetes.client.rest import ApiException

        loop = asyncio.get_running_loop()

        try:
            pods: Any = await loop.run_in_executor(
                None,
                lambda: core_api.list_namespaced_pod(
                    namespace=self._namespace,
                    label_selector=f"job-name={job_name}",
                ),
            )
            if not pods.items:
                return "", ""

            pod_name = pods.items[0].metadata.name
            log_text: str = await loop.run_in_executor(
                None,
                lambda: core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self._namespace,
                    container="forge-task",
                ),
            )
            return log_text, ""
        except ApiException as exc:
            logger.warning("Failed to collect logs for Job %s: %s", job_name, exc)
            return "", str(exc)

    async def _read_pod_exit_code(
        self,
        core_api: Any,
        job_name: str,
        *,
        default: int = 1,
    ) -> int:
        """Read the sandbox entrypoint exit code from the Job pod status."""
        from kubernetes.client.rest import ApiException

        loop = asyncio.get_running_loop()
        try:
            pods: Any = await loop.run_in_executor(
                None,
                lambda: core_api.list_namespaced_pod(
                    namespace=self._namespace,
                    label_selector=f"job-name={job_name}",
                ),
            )
            for pod in pods.items:
                for status in pod.status.container_statuses or []:
                    if status.name != "forge-task":
                        continue
                    terminated = status.state.terminated
                    if terminated is not None and terminated.exit_code is not None:
                        return int(terminated.exit_code)
        except (ApiException, AttributeError) as exc:
            logger.warning("Failed to read exit code for Job %s: %s", job_name, exc)
        return default

    async def _delete_job(self, batch_api: Any, job_name: str) -> None:
        """Delete a Job and its pods."""
        from kubernetes import client as k8s_client
        from kubernetes.client.rest import ApiException

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: batch_api.delete_namespaced_job(
                    name=job_name,
                    namespace=self._namespace,
                    body=k8s_client.V1DeleteOptions(
                        propagation_policy="Foreground",
                    ),
                ),
            )
            logger.info("Deleted Job %s", job_name)
        except ApiException as exc:
            logger.warning("Failed to delete Job %s: %s", job_name, exc)
