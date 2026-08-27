"""Jira effect executors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.integrations.jira.client import JiraClient
from forge.workflow.utils.jira_status import format_status_comment

JIRA_COMMENT_OPERATION = "jira.comment.create"
JIRA_LABEL_OPERATION = "jira.label.set"
JIRA_DESCRIPTION_OPERATION = "jira.description.update"
JIRA_CUSTOM_FIELD_OPERATION = "jira.custom_field.update"
JIRA_ATTACHMENT_REPLACE_OPERATION = "jira.attachment.replace"


class JiraCommentExecutor:
    operation = JIRA_COMMENT_OPERATION

    def __init__(self, client_factory: Callable[[], JiraClient] = JiraClient) -> None:
        self._client_factory = client_factory

    async def execute(self, command: EffectCommand) -> EffectResult:
        issue_key = command.target.external_id
        body = str(command.payload["body"])
        marker = f"forge-effect:{command.idempotency_key}"
        rendered = f"{format_status_comment(body)}\n\n{{{marker}}}"
        jira = self._client_factory()
        try:
            comments = await jira.get_comments(issue_key)
            existing = next((comment for comment in comments if marker in comment.body), None)
            if existing is None:
                created = await jira.add_comment(issue_key, rendered)
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
                created = await jira.add_attachment(
                    issue_key,
                    filename=filename,
                    content=str(command.payload["content"]),
                    content_type=str(command.payload.get("content_type", "text/plain")),
                )
                provider_reference = str(getattr(created, "id", filename))
            else:  # pragma: no cover - registry construction prevents this
                raise ValueError(f"Unsupported Jira effect operation: {self.operation}")
            return EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=EffectResultStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                provider_reference=provider_reference,
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
    ):
        registry.register(JiraMutationExecutor(operation))
