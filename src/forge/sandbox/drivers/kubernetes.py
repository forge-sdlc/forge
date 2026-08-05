"""Kubernetes Job-based sandbox driver."""

from __future__ import annotations

import asyncio
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
        self._workspace_pvc = settings.k8s_workspace_pvc
        self._workspace_base_path = settings.k8s_workspace_base_path
        self._image_pull_secrets = [
            s.strip() for s in settings.k8s_image_pull_secrets.split(",") if s.strip()
        ]
        self._service_account = settings.k8s_service_account
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

    async def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        k8s_config.load_config()
        batch_api = k8s_client.BatchV1Api()
        core_api = k8s_client.CoreV1Api()

        job_name = self._job_name(spec.container_name)
        prepared_spec, staging_dir = self._stage_external_mounts(spec)
        job_manifest = self._build_job_manifest(prepared_spec, job_name=job_name)

        logger.info(
            "Creating K8s Job %s in namespace %s",
            job_name,
            self._namespace,
        )

        job_created = False
        job_deleted = False
        try:
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

        env_vars = [k8s_client.V1EnvVar(name=k, value=v) for k, v in spec.env_vars.items()]

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

        container_args = [
            "--task-file",
            "/workspace/.forge/task.json",
            "--max-retries",
            str(spec.max_retries),
        ]
        if spec.skip_tests:
            container_args.append("--skip-tests")

        resources = k8s_client.V1ResourceRequirements(
            requests={"memory": spec.memory_limit, "cpu": spec.cpu_limit},
            limits={"memory": spec.memory_limit},
        )

        container = k8s_client.V1Container(
            name="forge-task",
            image=spec.image,
            args=container_args,
            env=env_vars,
            volume_mounts=volume_mounts,
            resources=resources,
            working_dir="/workspace",
        )

        pod_spec = k8s_client.V1PodSpec(
            containers=[container],
            volumes=volumes,
            restart_policy="Never",
            service_account_name=self._service_account or None,
        )

        if self._image_pull_secrets:
            pod_spec.image_pull_secrets = [
                k8s_client.V1LocalObjectReference(name=s) for s in self._image_pull_secrets
            ]

        template = k8s_client.V1PodTemplateSpec(
            metadata=k8s_client.V1ObjectMeta(
                labels={"app.kubernetes.io/managed-by": "forge"},
            ),
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
        staged_mounts: list[tuple[Path, str, str]] = []
        staging_dir: Path | None = None

        for index, (host_path, container_path, mode) in enumerate(spec.volume_mounts):
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
        if not self._workspace_base_path:
            return ""
        try:
            return str(workspace_path.relative_to(self._workspace_base_path))
        except ValueError:
            logger.warning(
                "Workspace %s is not under base path %s; mounting PVC root",
                workspace_path,
                self._workspace_base_path,
            )
            return ""

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
        try:
            conditions = job.status.conditions or []
            for cond in conditions:
                if (
                    cond.type == "Failed"
                    and cond.status == "True"
                    and cond.reason == "DeadlineExceeded"
                ):
                    return -1
        except AttributeError:
            pass
        return 1

    async def _collect_logs(
        self,
        core_api: Any,
        job_name: str,
    ) -> tuple[str, str]:
        """Retrieve stdout from the first pod of the Job."""
        from kubernetes import client as k8s_client

        loop = asyncio.get_running_loop()

        try:
            pods: k8s_client.V1PodList = await loop.run_in_executor(
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
        except k8s_client.ApiException as exc:
            logger.warning("Failed to collect logs for Job %s: %s", job_name, exc)
            return "", str(exc)

    async def _delete_job(self, batch_api: Any, job_name: str) -> None:
        """Delete a Job and its pods."""
        from kubernetes import client as k8s_client

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
        except k8s_client.ApiException as exc:
            logger.warning("Failed to delete Job %s: %s", job_name, exc)
