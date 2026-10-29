"""Durable effects for externally visible repository mutations."""

import asyncio
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.integrations.source_control.registry import Registry, get_registry
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

REPOSITORY_PUSH_OPERATION = "repository.ref.push"


class RepositoryPushExecutor:
    operation = REPOSITORY_PUSH_OPERATION
    # Before every attempt we read the remote ref; a timed-out successful push
    # is therefore observed as already applied and is not repeated.
    retry_safe_after_ambiguous_failure = True

    def __init__(self, registry_factory: Callable[[], Registry] = get_registry) -> None:
        self._registry_factory = registry_factory

    async def execute(self, command: EffectCommand) -> EffectResult:
        payload: dict[str, Any] = command.payload
        resolved = self._registry_factory().resolve(str(payload["repository"]))
        if resolved.adapter is None:
            raise RuntimeError(f"No adapter registered for {resolved.repo_ref.provider}")
        workspace = Workspace(
            path=Path(str(payload["workspace_path"])),
            repo_name=str(payload["repository"]),
            branch_name=str(payload["branch"]),
            ticket_key=str(payload["ticket_key"]),
        )
        credentials = await resolved.adapter.get_git_credentials(resolved.repo_ref)
        cancellation_event = threading.Event()
        git = GitOperations(workspace, credentials, cancellation_event=cancellation_event)
        remote = "fork" if bool(payload.get("use_fork")) else "origin"
        expected_sha = str(payload["commit_sha"])

        def push_if_needed() -> EffectResult:
            current_sha = git.get_current_sha()
            if current_sha != expected_sha:
                return EffectResult(
                    effect_id=command.effect_id,
                    idempotency_key=command.idempotency_key,
                    status=EffectResultStatus.SUCCEEDED,
                    completed_at=datetime.now(UTC),
                    provider_reference=f"superseded-by:{current_sha}",
                    output={"superseded_by": current_sha},
                )
            remote_sha = git.get_remote_branch_sha(workspace.branch_name, remote=remote)
            if remote_sha != expected_sha:
                if remote == "fork":
                    git.push_to_fork(force=bool(payload.get("force")))
                else:
                    git.push(
                        force=bool(payload.get("force")),
                        check_conflicts=bool(payload.get("check_conflicts", True)),
                    )
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                provider_reference=f"{remote}:{workspace.branch_name}@{expected_sha}",
            )

        return await _run_git_call(push_if_needed, cancellation_event)


async def _run_git_call(call, cancellation_event: threading.Event, *args, **kwargs):
    """Wait for a blocking Git call to stop before propagating task cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancellation_event.set()
        with suppress(BaseException):
            await asyncio.shield(worker)
            # Join the worker even when Git reports its cooperative stop as an
            # exception, then preserve cancellation for the effect service.
        raise


def register_repository_executors(registry: EffectExecutorRegistry) -> None:
    registry.register(RepositoryPushExecutor())
