"""Jira effect executors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.integrations.jira.client import JiraClient

JIRA_COMMENT_OPERATION = "jira.comment.create"
JIRA_LABEL_OPERATION = "jira.label.set"
JIRA_DESCRIPTION_OPERATION = "jira.description.update"
JIRA_CUSTOM_FIELD_OPERATION = "jira.custom_field.update"
JIRA_ATTACHMENT_REPLACE_OPERATION = "jira.attachment.replace"
JIRA_ATTACHMENT_ADD_OPERATION = "jira.attachment.add"
JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION = "jira.attachment.delete_by_name"
JIRA_STRUCTURED_COMMENT_OPERATION = "jira.structured_comment.create"
JIRA_TRANSITION_OPERATION = "jira.issue.transition"
JIRA_LABELS_ADD_OPERATION = "jira.labels.add"
JIRA_LABELS_REMOVE_OPERATION = "jira.labels.remove"
JIRA_ARCHIVE_OPERATION = "jira.issue.archive"
JIRA_PROJECT_PROPERTY_SET_OPERATION = "jira.project_property.set"
JIRA_PROJECT_PROPERTY_DELETE_OPERATION = "jira.project_property.delete"
JIRA_TASK_CREATE_OPERATION = "jira.task.create"
JIRA_EPIC_CREATE_OPERATION = "jira.epic.create"
JIRA_ISSUE_LINK_CREATE_OPERATION = "jira.issue_link.create"
JIRA_REMOTE_LINK_CREATE_OPERATION = "jira.remote_link.create"
JIRA_ERROR_COMMENT_OPERATION = "jira.error_comment.create"
JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION = "jira.model_policy_error_comment.create"
_EFFECT_PROPERTY = "forge.effect"


def _effect_property(idempotency_key: str) -> dict[str, str]:
    return {"idempotency_key": idempotency_key}


def _find_effect_comment(comments: list[Any], idempotency_key: str) -> Any | None:
    """Find a property-tagged comment while retaining recovery for old visible markers."""
    legacy_marker = f"forge-effect:{idempotency_key}"
    return next(
        (
            comment
            for comment in comments
            if getattr(comment, "properties", {}).get(_EFFECT_PROPERTY)
            == _effect_property(idempotency_key)
            or legacy_marker in comment.body
        ),
        None,
    )


class JiraCommentExecutor:
    operation = JIRA_COMMENT_OPERATION

    def __init__(self, client_factory: Callable[[], JiraClient] = JiraClient) -> None:
        self._client_factory = client_factory

    async def execute(self, command: EffectCommand) -> EffectResult:
        issue_key = command.target.external_id
        body = str(command.payload["body"])
        jira = self._client_factory()
        try:
            comments = await jira.get_comments(issue_key)
            existing = _find_effect_comment(comments, command.idempotency_key)
            if existing is None:
                created = await jira.add_comment(
                    issue_key,
                    body,
                    properties={_EFFECT_PROPERTY: _effect_property(command.idempotency_key)},
                )
                provider_reference = str(created.id)
            else:
                provider_reference = str(existing.id)
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                provider_reference=provider_reference,
            )
        finally:
            await jira.close()


class JiraMutationExecutor:
    """Execute naturally idempotent Jira mutations from durable intent."""

    def __init__(
        self,
        operation: str,
        client_factory: Callable[[], JiraClient] = JiraClient,
    ) -> None:
        self.operation = operation
        self._client_factory = client_factory

    async def execute(self, command: EffectCommand) -> EffectResult:
        issue_key = command.target.external_id
        jira = self._client_factory()
        provider_reference: str | None = issue_key
        output: dict[str, Any] = {}
        try:
            if self.operation == JIRA_LABEL_OPERATION:
                await jira.set_workflow_label(issue_key, str(command.payload["label"]))
            elif self.operation == JIRA_DESCRIPTION_OPERATION:
                await jira.update_description(issue_key, str(command.payload["description"]))
            elif self.operation == JIRA_CUSTOM_FIELD_OPERATION:
                await jira.update_custom_field(
                    issue_key,
                    str(command.payload["field"]),
                    str(command.payload["value"]),
                )
            elif self.operation == JIRA_ATTACHMENT_REPLACE_OPERATION:
                filename = str(command.payload["filename"])
                await jira.delete_attachments_by_name(issue_key, filename)
                replacement = await jira.add_attachment(
                    issue_key,
                    filename=filename,
                    content=str(command.payload["content"]),
                    content_type=str(command.payload.get("content_type", "text/plain")),
                )
                provider_reference = _provider_id(replacement, filename)
            elif self.operation == JIRA_ATTACHMENT_ADD_OPERATION:
                attachment = await jira.add_attachment(
                    issue_key,
                    filename=str(command.payload["filename"]),
                    content=str(command.payload["content"]),
                    content_type=str(command.payload.get("content_type", "text/markdown")),
                )
                provider_reference = _provider_id(attachment, str(command.payload["filename"]))
                output = dict(attachment) if isinstance(attachment, dict) else {}
            elif self.operation == JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION:
                deleted = await jira.delete_attachments_by_name(
                    issue_key, str(command.payload["filename"])
                )
                output = {"deleted": deleted}
            elif self.operation == JIRA_STRUCTURED_COMMENT_OPERATION:
                comments = await jira.get_comments(issue_key)
                existing = _find_effect_comment(comments, command.idempotency_key)
                if existing is None:
                    structured_comment = await jira.add_structured_comment(
                        issue_key,
                        str(command.payload["title"]),
                        str(command.payload["content"]),
                        comment_type=str(command.payload["comment_type"]),
                        properties={_EFFECT_PROPERTY: _effect_property(command.idempotency_key)},
                    )
                    provider_reference = str(structured_comment.id)
                else:
                    provider_reference = str(existing.id)
            elif self.operation == JIRA_TRANSITION_OPERATION:
                transition = str(command.payload["transition"])
                issue = await jira.get_issue(issue_key)
                if issue.status.lower() != transition.lower():
                    await jira.transition_issue(issue_key, transition)
            elif self.operation == JIRA_LABELS_ADD_OPERATION:
                requested = _string_list(command.payload["labels"])
                current = set(await jira.get_labels(issue_key))
                missing = [label for label in requested if label not in current]
                if missing:
                    await jira.add_labels(issue_key, missing)
            elif self.operation == JIRA_LABELS_REMOVE_OPERATION:
                requested = _string_list(command.payload["labels"])
                current = set(await jira.get_labels(issue_key))
                present = [label for label in requested if label in current]
                if present:
                    await jira.remove_labels(issue_key, present)
            elif self.operation == JIRA_ARCHIVE_OPERATION:
                await jira.archive_issue(
                    issue_key, archive_subtasks=bool(command.payload.get("archive_subtasks", True))
                )
            elif self.operation == JIRA_PROJECT_PROPERTY_SET_OPERATION:
                await jira.set_project_property(
                    issue_key,
                    str(command.payload["property_key"]),
                    command.payload["value"],
                )
            elif self.operation == JIRA_PROJECT_PROPERTY_DELETE_OPERATION:
                await jira.delete_project_property(issue_key, str(command.payload["property_key"]))
            elif self.operation in {JIRA_TASK_CREATE_OPERATION, JIRA_EPIC_CREATE_OPERATION}:
                marker = _creation_marker(command.idempotency_key)
                existing_issues = await jira.search_issues(
                    f'project = "{command.payload["project_key"]}" AND labels = "{marker}"',
                    fields=["summary", "labels"],
                    max_results=2,
                )
                if len(existing_issues) > 1:
                    raise RuntimeError(f"Creation marker {marker} resolves to multiple Jira issues")
                if existing_issues:
                    provider_reference = existing_issues[0].key
                else:
                    labels = _string_list(command.payload.get("labels", []))
                    labels.append(marker)
                    if self.operation == JIRA_TASK_CREATE_OPERATION:
                        provider_reference = await jira.create_task(
                            str(command.payload["project_key"]),
                            str(command.payload["summary"]),
                            str(command.payload["description"]),
                            parent_key=_optional_string(command.payload.get("parent_key")),
                            labels=labels,
                        )
                    else:
                        provider_reference = await jira.create_epic(
                            str(command.payload["project_key"]),
                            str(command.payload["summary"]),
                            str(command.payload["description"]),
                            str(command.payload["parent_key"]),
                            labels=labels,
                        )
            elif self.operation == JIRA_ISSUE_LINK_CREATE_OPERATION:
                inward_key = str(command.payload["inward_key"])
                outward_key = str(command.payload["outward_key"])
                link_type = str(command.payload["link_type"])
                links = await jira.get_issue_links(inward_key)
                exists = any(
                    str(link.get("type", "")).lower() == link_type.lower()
                    and {
                        str(link.get("inward_key") or ""),
                        str(link.get("outward_key") or ""),
                    }
                    == {inward_key, outward_key}
                    for link in links
                )
                if not exists:
                    await jira.create_issue_link(link_type, inward_key, outward_key)
                provider_reference = f"{inward_key}:{link_type}:{outward_key}"
            elif self.operation == JIRA_REMOTE_LINK_CREATE_OPERATION:
                url = str(command.payload["url"])
                title = str(command.payload["title"])
                remote_links = await jira.get_remote_links(issue_key)
                if not any(link.get("url") == url for link in remote_links):
                    await jira.create_remote_link(issue_key, url, title)
                provider_reference = url
            elif self.operation == JIRA_ERROR_COMMENT_OPERATION:
                comments = await jira.get_comments(issue_key)
                existing = _find_effect_comment(comments, command.idempotency_key)
                if existing is None:
                    error_comment = await jira.add_error_comment(
                        issue_key,
                        str(command.payload["error_message"]),
                        str(command.payload["node_name"]),
                        mention_account_ids=[
                            *_string_list(command.payload.get("mention_account_ids", []))
                        ],
                        properties={_EFFECT_PROPERTY: _effect_property(command.idempotency_key)},
                    )
                    provider_reference = str(error_comment.id)
                else:
                    provider_reference = str(existing.id)
            elif self.operation == JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION:
                comments = await jira.get_comments(issue_key)
                existing = _find_effect_comment(comments, command.idempotency_key)
                if existing is None:
                    policy_comment = await jira.add_model_policy_error_comment(
                        issue_key,
                        str(command.payload["node_name"]),
                        str(command.payload["problem"]),
                        str(command.payload["available_connections"]),
                        str(command.payload["fix_command"]),
                        mention_account_ids=[
                            *_string_list(command.payload.get("mention_account_ids", []))
                        ],
                        properties={_EFFECT_PROPERTY: _effect_property(command.idempotency_key)},
                    )
                    provider_reference = str(policy_comment.id)
                else:
                    provider_reference = str(existing.id)
            else:  # pragma: no cover - registry construction prevents this
                raise ValueError(f"Unsupported Jira effect operation: {self.operation}")
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                provider_reference=provider_reference,
                output=output,
            )
        finally:
            await jira.close()


def register_jira_executors(registry: EffectExecutorRegistry) -> None:
    registry.register(JiraCommentExecutor())
    for operation in (
        JIRA_LABEL_OPERATION,
        JIRA_DESCRIPTION_OPERATION,
        JIRA_CUSTOM_FIELD_OPERATION,
        JIRA_ATTACHMENT_REPLACE_OPERATION,
        JIRA_ATTACHMENT_ADD_OPERATION,
        JIRA_ATTACHMENT_DELETE_BY_NAME_OPERATION,
        JIRA_STRUCTURED_COMMENT_OPERATION,
        JIRA_TRANSITION_OPERATION,
        JIRA_LABELS_ADD_OPERATION,
        JIRA_LABELS_REMOVE_OPERATION,
        JIRA_ARCHIVE_OPERATION,
        JIRA_PROJECT_PROPERTY_SET_OPERATION,
        JIRA_PROJECT_PROPERTY_DELETE_OPERATION,
        JIRA_TASK_CREATE_OPERATION,
        JIRA_EPIC_CREATE_OPERATION,
        JIRA_ISSUE_LINK_CREATE_OPERATION,
        JIRA_REMOTE_LINK_CREATE_OPERATION,
        JIRA_ERROR_COMMENT_OPERATION,
        JIRA_MODEL_POLICY_ERROR_COMMENT_OPERATION,
    ):
        registry.register(JiraMutationExecutor(operation))


def _creation_marker(idempotency_key: str) -> str:
    """Return a Jira-label-safe recovery marker for create crash windows."""
    digest = idempotency_key.rsplit(":", 1)[-1]
    return f"forge-effect-{digest[:40]}"


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Expected a list")
    return [str(item) for item in value]


def _provider_id(value: object, fallback: str) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or fallback)
    return str(getattr(value, "id", fallback))
