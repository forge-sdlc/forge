"""Jira effect executors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.integrations.jira.client import JiraClient
from forge.workflow.utils.jira_status import format_status_comment

JIRA_COMMENT_OPERATION = "jira.comment.create"


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


def register_jira_executors(registry: EffectExecutorRegistry) -> None:
    registry.register(JiraCommentExecutor())
