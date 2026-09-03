"""Workflow-facing provider ports backed by the durable effect journal.

Reads are delegated to the provider client. Writes are converted to durable,
idempotent effects before an executor is allowed to call the provider.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, replace
from enum import Enum
from typing import Any, cast

from forge.config import Settings
from forge.domain import EffectCommand, ResourceIdentity, WorkflowIdentity, stable_identity
from forge.domain.schema import JsonValue
from forge.effects.executors import EffectExecutorRegistry
from forge.effects.jira import (
    JIRA_ARCHIVE_OPERATION,
    JIRA_ATTACHMENT_ADD_OPERATION,
    JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION,
    JIRA_COMMENT_OPERATION,
    JIRA_CUSTOM_FIELD_OPERATION,
    JIRA_DESCRIPTION_OPERATION,
    JIRA_EPIC_CREATE_OPERATION,
    JIRA_ERROR_COMMENT_OPERATION,
    JIRA_ISSUE_LINK_CREATE_OPERATION,
    JIRA_LABEL_OPERATION,
    JIRA_LABELS_ADD_OPERATION,
    JIRA_LABELS_REMOVE_OPERATION,
    JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION,
    JIRA_REMOTE_LINK_CREATE_OPERATION,
    JIRA_STRUCTURED_COMMENT_OPERATION,
    JIRA_TASK_CREATE_OPERATION,
    JIRA_TRANSITION_OPERATION,
    JiraMutationExecutor,
)
from forge.effects.journal import InMemoryEffectJournal
from forge.effects.repository import REPOSITORY_PUSH_OPERATION
from forge.effects.service import EffectService, RequiredEffectError
from forge.effects.source_control import (
    SC_BRANCH_CREATE_OPERATION,
    SC_CHANGE_REQUEST_CREATE_OPERATION,
    SC_CHANGE_REQUEST_UPDATE_OPERATION,
    SC_COMMENT_CREATE_OPERATION,
    SC_COMMENT_REPLY_OPERATION,
    SC_FILE_PUT_OPERATION,
    SourceControlMutationExecutor,
)
from forge.integrations.jira.client import JiraClient as ProviderJiraClient
from forge.integrations.jira.models import JiraComment
from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    RepositoryRef,
    ResolvedRepository,
    ReviewComment,
    WriteTarget,
)
from forge.workflow.declarative.capabilities import require_effect_capability
from forge.workspace.git_ops import GitOperations

_service: ContextVar[EffectService | None] = ContextVar("workflow_effect_service", default=None)
_identity: ContextVar[WorkflowIdentity | None] = ContextVar(
    "workflow_effect_identity", default=None
)


@contextmanager
def bind_effect_runtime(service: EffectService, identity: WorkflowIdentity) -> Iterator[None]:
    """Bind the control-plane effect runtime while invoking workflow code."""
    service_token = _service.set(service)
    identity_token = _identity.set(identity)
    try:
        yield
    finally:
        _identity.reset(identity_token)
        _service.reset(service_token)


class _BorrowedJiraClient:
    """Prevent a locally-owned executor from closing the node's read client."""

    def __init__(self, client: ProviderJiraClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def close(self) -> None:
        return None


class JiraClient:
    """Workflow Jira port: provider reads plus journalled provider writes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._provider = ProviderJiraClient(settings)
        self._local_service: EffectService | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def close(self) -> None:
        await self._provider.close()

    def _runtime(self) -> EffectService:
        bound = _service.get()
        if bound is not None:
            return bound
        if self._local_service is None:
            registry = EffectExecutorRegistry()
            for operation in (
                JIRA_COMMENT_OPERATION,
                JIRA_LABEL_OPERATION,
                JIRA_DESCRIPTION_OPERATION,
                JIRA_CUSTOM_FIELD_OPERATION,
                JIRA_ATTACHMENT_ADD_OPERATION,
                JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION,
                JIRA_STRUCTURED_COMMENT_OPERATION,
                JIRA_TRANSITION_OPERATION,
                JIRA_LABELS_ADD_OPERATION,
                JIRA_LABELS_REMOVE_OPERATION,
                JIRA_ARCHIVE_OPERATION,
                JIRA_TASK_CREATE_OPERATION,
                JIRA_EPIC_CREATE_OPERATION,
                JIRA_ERROR_COMMENT_OPERATION,
                JIRA_ISSUE_LINK_CREATE_OPERATION,
                JIRA_REMOTE_LINK_CREATE_OPERATION,
                JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION,
            ):
                registry.register(
                    JiraMutationExecutor(
                        operation,
                        client_factory=cast(Any, lambda: _BorrowedJiraClient(self._provider)),
                    )
                )
            self._local_service = EffectService(InMemoryEffectJournal(), registry)
        return self._local_service

    async def _write(
        self,
        operation: str,
        issue_key: str,
        payload: dict[str, JsonValue],
    ) -> Any:
        caller = inspect.currentframe()
        for _ in range(2):
            caller = caller.f_back if caller is not None else None
        location = (
            f"{caller.f_globals.get('__name__', 'unknown')}.{caller.f_code.co_name}"
            if caller is not None
            else "unknown"
        )
        normalized = _json_value(payload)
        identity = _identity.get() or WorkflowIdentity(
            run_id=issue_key,
            workflow_name="local",
            definition_revision=1,
        )
        parts: dict[str, JsonValue] = {
            "run_id": identity.run_id,
            "definition_revision": identity.definition_revision,
            "operation": operation,
            "target": issue_key,
            "payload": normalized,
            "origin": location,
        }
        effect_id = stable_identity("effect", parts)
        require_effect_capability(operation)
        record = await self._runtime().execute_required(
            EffectCommand(
                effect_id=effect_id,
                idempotency_key=effect_id,
                workflow=identity,
                operation=operation,
                target=ResourceIdentity(resource_type="issue", external_id=issue_key),
                payload=normalized,
            )
        )
        if record.result is None:  # pragma: no cover - execute_required contract
            raise RequiredEffectError(record)
        return record.result

    async def add_comment(self, issue_key: str, body: str) -> JiraComment:
        result = await self._write(JIRA_COMMENT_OPERATION, issue_key, {"body": body})
        return JiraComment(
            id=str(result.provider_reference or ""), body=body, author_id="", author_name=""
        )

    async def add_structured_comment(
        self, issue_key: str, title: str, content: str, comment_type: str = "forge-artifact"
    ) -> JiraComment:
        result = await self._write(
            JIRA_STRUCTURED_COMMENT_OPERATION,
            issue_key,
            {"title": title, "content": content, "comment_type": comment_type},
        )
        return JiraComment(
            id=str(result.provider_reference or ""), body=content, author_id="", author_name=""
        )

    async def set_workflow_label(
        self, issue_key: str, new_label: Any, remove_prefix: str = "forge:"
    ) -> None:
        await self._write(
            JIRA_LABEL_OPERATION,
            issue_key,
            {"label": _json_value(new_label), "remove_prefix": remove_prefix},
        )

    async def update_description(self, issue_key: str, description: str) -> None:
        await self._write(JIRA_DESCRIPTION_OPERATION, issue_key, {"description": description})

    async def update_custom_field(self, issue_key: str, field_id: str, value: str) -> None:
        await self._write(
            JIRA_CUSTOM_FIELD_OPERATION, issue_key, {"field": field_id, "value": value}
        )

    async def transition_issue(self, issue_key: str, transition_name: str) -> None:
        await self._write(JIRA_TRANSITION_OPERATION, issue_key, {"transition": transition_name})

    async def add_labels(
        self,
        issue_key: str,
        labels: list[str],
        *,
        effect_scope: str | None = None,
    ) -> None:
        payload: dict[str, JsonValue] = {"labels": labels}
        if effect_scope:
            payload["effect_scope"] = effect_scope
        await self._write(JIRA_LABELS_ADD_OPERATION, issue_key, payload)

    async def remove_labels(self, issue_key: str, labels: list[str]) -> None:
        await self._write(JIRA_LABELS_REMOVE_OPERATION, issue_key, {"labels": labels})

    async def archive_issue(self, issue_key: str, archive_subtasks: bool = True) -> None:
        await self._write(
            JIRA_ARCHIVE_OPERATION,
            issue_key,
            {"archive_subtasks": archive_subtasks},
        )

    async def create_task(
        self,
        project_key: str,
        summary: str,
        description: str,
        parent_key: str | None = None,
        labels: list[str] | None = None,
    ) -> str:
        result = await self._write(
            JIRA_TASK_CREATE_OPERATION,
            parent_key or project_key,
            {
                "project_key": project_key,
                "summary": summary,
                "description": description,
                "parent_key": parent_key,
                "labels": labels or [],
            },
        )
        return str(result.provider_reference)

    async def create_epic(
        self,
        project_key: str,
        summary: str,
        description: str,
        parent_key: str,
        labels: list[str] | None = None,
    ) -> str:
        result = await self._write(
            JIRA_EPIC_CREATE_OPERATION,
            parent_key,
            {
                "project_key": project_key,
                "summary": summary,
                "description": description,
                "parent_key": parent_key,
                "labels": labels or [],
            },
        )
        return str(result.provider_reference)

    async def create_issue_link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        await self._write(
            JIRA_ISSUE_LINK_CREATE_OPERATION,
            inward_key,
            {
                "link_type": link_type,
                "inward_key": inward_key,
                "outward_key": outward_key,
            },
        )

    async def create_remote_link(self, issue_key: str, url: str, title: str) -> None:
        await self._write(
            JIRA_REMOTE_LINK_CREATE_OPERATION, issue_key, {"url": url, "title": title}
        )

    async def add_attachment(
        self,
        issue_key: str,
        filename: str,
        content: str | bytes,
        content_type: str = "text/markdown",
    ) -> dict[str, Any]:
        result = await self._write(
            JIRA_ATTACHMENT_ADD_OPERATION,
            issue_key,
            {
                "filename": filename,
                "content": content.decode() if isinstance(content, bytes) else content,
                "content_type": content_type,
            },
        )
        return dict(result.output)

    async def delete_attachments_by_name(self, issue_key: str, filename: str) -> int:
        result = await self._write(
            JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION, issue_key, {"filename": filename}
        )
        return int(result.output.get("deleted", 0))

    async def add_error_comment(
        self,
        issue_key: str,
        error_message: str,
        node_name: str,
        mention_account_ids: list[str] | None = None,
    ) -> JiraComment:
        result = await self._write(
            JIRA_ERROR_COMMENT_OPERATION,
            issue_key,
            {
                "error_message": error_message,
                "node_name": node_name,
                "mention_account_ids": mention_account_ids or [],
            },
        )
        return JiraComment(
            id=str(result.provider_reference or ""),
            body=error_message,
            author_id="",
            author_name="",
        )

    async def add_model_policy_error_comment(
        self,
        issue_key: str,
        node_name: str,
        problem: str,
        available_connections: str,
        fix_command: str,
        mention_account_ids: list[str] | None = None,
    ) -> JiraComment:
        result = await self._write(
            JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION,
            issue_key,
            {
                "node_name": node_name,
                "problem": problem,
                "available_connections": available_connections,
                "fix_command": fix_command,
                "mention_account_ids": mention_account_ids or [],
            },
        )
        return JiraComment(
            id=str(result.provider_reference or ""),
            body=problem,
            author_id="",
            author_name="",
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


async def push_repository(
    git: GitOperations,
    *,
    use_fork: bool,
    force: bool = False,
    check_conflicts: bool | None = None,
) -> None:
    """Persist a ref-update intent before pushing a workspace branch."""
    service = _service.get()
    if service is None:
        if use_fork:
            if force:
                git.push_to_fork(force=True)
            else:
                git.push_to_fork()
        elif check_conflicts is None:
            git.push(force=force)
        else:
            git.push(force=force, check_conflicts=check_conflicts)
        return
    identity = _identity.get()
    if identity is None:  # pragma: no cover - bound as one context
        raise RuntimeError("Workflow identity is not bound")
    commit_sha = git.get_current_sha()
    payload: dict[str, JsonValue] = {
        "workspace_path": str(git.workspace.path),
        "repository": git.workspace.repo_name,
        "branch": git.workspace.branch_name,
        "ticket_key": git.workspace.ticket_key,
        "commit_sha": commit_sha,
        "use_fork": use_fork,
        "force": force,
        "check_conflicts": True if check_conflicts is None else check_conflicts,
    }
    effect_id = stable_identity(
        "effect",
        {
            "run_id": identity.run_id,
            "operation": REPOSITORY_PUSH_OPERATION,
            "repository": git.workspace.repo_name,
            "branch": git.workspace.branch_name,
            "commit_sha": commit_sha,
        },
    )
    require_effect_capability(REPOSITORY_PUSH_OPERATION)
    await service.execute_required(
        EffectCommand(
            effect_id=effect_id,
            idempotency_key=effect_id,
            workflow=identity,
            operation=REPOSITORY_PUSH_OPERATION,
            target=ResourceIdentity(
                resource_type="repository_ref",
                external_id=git.workspace.branch_name,
                namespace=git.workspace.repo_name,
            ),
            payload=payload,
        )
    )


class _SingleRepositoryRegistry:
    def __init__(self, resolved: ResolvedRepository) -> None:
        self._resolved = resolved

    def resolve(self, identifier: str) -> ResolvedRepository:
        return replace(
            self._resolved,
            repo_ref=replace(self._resolved.repo_ref, id=identifier, namespace=identifier),
        )


class SourceControlAdapter:
    """Workflow source-control port with journalled mutations."""

    def __init__(self, resolved: ResolvedRepository) -> None:
        if resolved.adapter is None:
            raise ValueError("A source-control adapter is required")
        self._resolved = resolved
        self._provider = resolved.adapter
        self._local_service: EffectService | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def _runtime(self) -> EffectService:
        bound = _service.get()
        if bound is not None:
            return bound
        if self._local_service is None:
            registry = EffectExecutorRegistry()
            local_registry = _SingleRepositoryRegistry(self._resolved)
            for operation in (
                SC_BRANCH_CREATE_OPERATION,
                SC_FILE_PUT_OPERATION,
                SC_CHANGE_REQUEST_CREATE_OPERATION,
                SC_CHANGE_REQUEST_UPDATE_OPERATION,
                SC_COMMENT_CREATE_OPERATION,
                SC_COMMENT_REPLY_OPERATION,
            ):
                registry.register(
                    SourceControlMutationExecutor(operation, cast(Any, lambda: local_registry))
                )
            self._local_service = EffectService(InMemoryEffectJournal(), registry)
        return self._local_service

    async def _write(
        self,
        operation: str,
        repo_ref: RepositoryRef,
        external_id: str,
        payload: dict[str, JsonValue],
    ) -> Any:
        caller = inspect.currentframe()
        for _ in range(2):
            caller = caller.f_back if caller is not None else None
        location = (
            f"{caller.f_globals.get('__name__', 'unknown')}.{caller.f_code.co_name}"
            if caller is not None
            else "unknown"
        )
        normalized = _json_value(
            {
                **payload,
                "_repository_id": self._resolved.repo_ref.id,
                "_target_namespace": repo_ref.namespace,
            }
        )
        identity = _identity.get() or WorkflowIdentity(
            run_id=external_id or repo_ref.namespace,
            workflow_name="local",
            definition_revision=1,
        )
        effect_id = stable_identity(
            "effect",
            {
                "run_id": identity.run_id,
                "definition_revision": identity.definition_revision,
                "operation": operation,
                "repository": repo_ref.namespace,
                "target": external_id,
                "payload": normalized,
                "origin": location,
            },
        )
        require_effect_capability(operation)
        record = await self._runtime().execute_required(
            EffectCommand(
                effect_id=effect_id,
                idempotency_key=effect_id,
                workflow=identity,
                operation=operation,
                target=ResourceIdentity(
                    resource_type="change_request",
                    external_id=external_id,
                    namespace=repo_ref.namespace,
                ),
                payload=normalized,
            )
        )
        if record.result is None:  # pragma: no cover
            raise RequiredEffectError(record)
        return record.result

    async def create_branch(self, repo_ref: RepositoryRef, name: str, base: str) -> None:
        await self._write(SC_BRANCH_CREATE_OPERATION, repo_ref, name, {"name": name, "base": base})

    async def put_file(
        self,
        repo_ref: RepositoryRef,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> None:
        await self._write(
            SC_FILE_PUT_OPERATION,
            repo_ref,
            f"{branch}:{path}",
            {"path": path, "content": content, "message": message, "branch": branch},
        )

    async def create_change_request(
        self,
        repo_ref: RepositoryRef,
        target: WriteTarget,
        title: str,
        body: str,
        draft: bool = False,
    ) -> ChangeRequest:
        result = await self._write(
            SC_CHANGE_REQUEST_CREATE_OPERATION,
            repo_ref,
            target.head_ref,
            {"target": asdict(target), "title": title, "body": body, "draft": draft},
        )
        native_id = result.output.get("number") or result.provider_reference
        return ChangeRequest(
            identity=ChangeRequestIdentity(
                connection=repo_ref.connection,
                repository_id=repo_ref.id,
                native_id=str(native_id) if native_id is not None else None,
            ),
            url=str(result.output.get("url") or ""),
            title=title,
            body=body,
            state=ChangeRequestState.OPEN,
            source_branch=target.head_ref,
            target_branch=target.base_branch,
            draft=draft,
            created=bool(result.output.get("created", True)),
        )

    async def update_change_request(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        *,
        title: str | None = None,
        body: str | None = None,
        state: ChangeRequestState | None = None,
    ) -> ChangeRequest:
        result = await self._write(
            SC_CHANGE_REQUEST_UPDATE_OPERATION,
            repo_ref,
            str(identity.native_id),
            {"title": title, "body": body, "state": state.value if state else None},
        )
        return ChangeRequest(
            identity=identity,
            url=str(result.output.get("url") or ""),
            title=title or "",
            body=body or "",
            state=state or ChangeRequestState.OPEN,
            source_branch="",
            target_branch="",
        )

    async def create_comment(
        self, repo_ref: RepositoryRef, identity: ChangeRequestIdentity, body: str
    ) -> ReviewComment:
        result = await self._write(
            SC_COMMENT_CREATE_OPERATION, repo_ref, str(identity.native_id), {"body": body}
        )
        return ReviewComment(id=str(result.provider_reference or ""), body=body, author="forge")

    async def reply_to_comment(
        self,
        repo_ref: RepositoryRef,
        identity: ChangeRequestIdentity,
        comment_id: str,
        body: str,
    ) -> ReviewComment:
        result = await self._write(
            SC_COMMENT_REPLY_OPERATION,
            repo_ref,
            str(identity.native_id),
            {"comment_id": comment_id, "body": body},
        )
        return ReviewComment(
            id=str(result.provider_reference or ""),
            body=body,
            author="forge",
            in_reply_to=comment_id,
        )
