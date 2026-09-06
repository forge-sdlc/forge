"""Provider-neutral source-control effect executors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    ChangeRequestState,
    ResolvedRepository,
    WriteTarget,
)
from forge.integrations.source_control.errors import NotFoundError
from forge.integrations.source_control.registry import Registry, get_registry

SC_BRANCH_CREATE_OPERATION = "source_control.branch.create"
SC_FILE_PUT_OPERATION = "source_control.file.put"
SC_CHANGE_REQUEST_CREATE_OPERATION = "source_control.change_request.create"
SC_CHANGE_REQUEST_UPDATE_OPERATION = "source_control.change_request.update"
SC_COMMENT_CREATE_OPERATION = "source_control.comment.create"
SC_COMMENT_REPLY_OPERATION = "source_control.comment.reply"


class SourceControlMutationExecutor:
    """Execute a source-control mutation after resolving its registered repository."""

    def __init__(
        self,
        operation: str,
        registry_factory: Callable[[], Registry] = get_registry,
    ) -> None:
        self.operation = operation
        self._registry_factory = registry_factory

    async def execute(self, command: EffectCommand) -> EffectResult:
        resolved = self._registry_factory().resolve(
            str(
                command.payload.get("_repository_id")
                or command.target.namespace
                or command.target.external_id
            )
        )
        target_namespace = command.payload.get("_target_namespace")
        if target_namespace:
            resolved = replace(
                resolved,
                repo_ref=replace(
                    resolved.repo_ref,
                    id=str(target_namespace),
                    namespace=str(target_namespace),
                ),
            )
        adapter = resolved.adapter
        if adapter is None:
            raise RuntimeError(f"No adapter registered for {resolved.repo_ref.provider}")

        reference: str | None = command.target.external_id
        output: dict[str, Any] = {}
        if self.operation == SC_BRANCH_CREATE_OPERATION:
            await adapter.create_branch(
                resolved.repo_ref,
                str(command.payload["name"]),
                str(command.payload["base"]),
            )
            reference = str(command.payload["name"])
        elif self.operation == SC_FILE_PUT_OPERATION:
            path = str(command.payload["path"])
            content = str(command.payload["content"])
            branch = str(command.payload["branch"])
            try:
                current_content = await adapter.get_file(resolved.repo_ref, path, branch)
            except NotFoundError:
                current_content = None
            if current_content != content:
                await adapter.put_file(
                    resolved.repo_ref,
                    path,
                    content,
                    str(command.payload["message"]),
                    branch,
                )
            reference = f"{command.payload['branch']}:{command.payload['path']}"
        elif self.operation == SC_CHANGE_REQUEST_CREATE_OPERATION:
            target = WriteTarget(**cast(dict[str, Any], command.payload["target"]))
            change = await adapter.create_change_request(
                resolved.repo_ref,
                target,
                str(command.payload["title"]),
                str(command.payload["body"]),
                bool(command.payload.get("draft", False)),
            )
            reference = str(change.identity.native_id)
            output = {"url": change.url, "number": reference, "created": change.created}
        else:
            identity = _identity(resolved, command)
            if self.operation == SC_CHANGE_REQUEST_UPDATE_OPERATION:
                state_value = command.payload.get("state")
                change = await adapter.update_change_request(
                    resolved.repo_ref,
                    identity,
                    title=_optional_string(command.payload.get("title")),
                    body=_optional_string(command.payload.get("body")),
                    state=ChangeRequestState(str(state_value)) if state_value else None,
                )
                reference = str(change.identity.native_id)
                output = {"url": change.url, "number": reference}
            elif self.operation in {SC_COMMENT_CREATE_OPERATION, SC_COMMENT_REPLY_OPERATION}:
                marker = f"forge-effect:{command.idempotency_key}"
                if self.operation == SC_COMMENT_REPLY_OPERATION:
                    threads = await adapter.get_review_thread_comments(resolved.repo_ref, identity)
                    comments = [comment for thread in threads for comment in thread.comments]
                else:
                    comments = await adapter.get_change_request_comments(
                        resolved.repo_ref, identity
                    )
                existing = next((item for item in comments if marker in item.body), None)
                if existing is None:
                    body = f"{command.payload['body']}\n\n<!-- {marker} -->"
                    if self.operation == SC_COMMENT_CREATE_OPERATION:
                        existing = await adapter.create_comment(resolved.repo_ref, identity, body)
                    else:
                        existing = await adapter.reply_to_comment(
                            resolved.repo_ref,
                            identity,
                            str(command.payload["comment_id"]),
                            body,
                        )
                reference = existing.id
            else:  # pragma: no cover - registry construction prevents this
                raise ValueError(f"Unsupported source-control effect operation: {self.operation}")

        return EffectResult(
            effect_id=command.effect_id,
            idempotency_key=command.idempotency_key,
            status=EffectResultStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            provider_reference=reference,
            output=output,
        )


def _identity(resolved: ResolvedRepository, command: EffectCommand) -> ChangeRequestIdentity:
    return ChangeRequestIdentity(
        connection=resolved.repo_ref.connection,
        repository_id=resolved.repo_ref.id,
        native_id=command.target.external_id,
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def register_source_control_executors(registry: EffectExecutorRegistry) -> None:
    for operation in (
        SC_BRANCH_CREATE_OPERATION,
        SC_FILE_PUT_OPERATION,
        SC_CHANGE_REQUEST_CREATE_OPERATION,
        SC_CHANGE_REQUEST_UPDATE_OPERATION,
        SC_COMMENT_CREATE_OPERATION,
        SC_COMMENT_REPLY_OPERATION,
    ):
        registry.register(SourceControlMutationExecutor(operation))
