"""Immutable process-definition publication and explicit rollout decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from forge.domain import DomainModel
from forge.orchestrator.checkpointer import get_redis_client
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.manifest import ProcessChangeImpact, compare_process_definitions
from forge.workflow.declarative.models import WorkflowDefinition

_DEFINITION_PREFIX = "forge:process:def:"
_ACTIVE_PREFIX = "forge:process:active:"
_DECISIONS_PREFIX = "forge:process:decisions:"

_PUBLISH_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing and existing ~= ARGV[1] then
  return -1
end
if not existing then
  redis.call('SET', KEYS[1], ARGV[1])
end
if ARGV[2] == '1' then
  local active = redis.call('GET', KEYS[2])
  if ARGV[3] ~= '' and active and active ~= ARGV[3] then
    return -2
  end
  redis.call('SET', KEYS[2], ARGV[4])
end
redis.call('RPUSH', KEYS[3], ARGV[5])
return existing and 0 or 1
"""


class PublicationDecision(DomainModel):
    workflow_name: str
    revision: int
    digest: str
    published_at: datetime
    activated: bool
    actor: str
    impact: dict[str, Any] = Field(default_factory=dict)


class DefinitionPublisher:
    """Publish immutable revisions; activation is a separate CAS-protected decision."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def publish(
        self,
        definition: WorkflowDefinition,
        *,
        actor: str,
        activate: bool = False,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        DeclarativeWorkflowCompiler(definition).validate()
        previous = await self.active(definition.metadata.name)
        impact: ProcessChangeImpact | None = None
        if previous is not None:
            impact = compare_process_definitions(previous, definition)
            if activate and not impact.compatible_for_in_flight:
                raise ValueError("definition is incompatible with active workflow instances")
        decision = PublicationDecision(
            workflow_name=definition.metadata.name,
            revision=definition.metadata.revision,
            digest=definition.digest,
            published_at=datetime.now(UTC),
            activated=activate,
            actor=actor,
            impact=impact.model_dump(mode="json") if impact else {},
        )
        result = await (await self._client()).eval(
            _PUBLISH_SCRIPT,
            3,
            self._definition_key(definition.metadata.name, definition.metadata.revision),
            f"{_ACTIVE_PREFIX}{definition.metadata.name}",
            f"{_DECISIONS_PREFIX}{definition.metadata.name}",
            definition.canonical_json(),
            "1" if activate else "0",
            expected_active_digest or "",
            f"{definition.metadata.revision}:{definition.digest}",
            decision.model_dump_json(),
        )
        if result == -1:
            raise ValueError("published revision is immutable and has different content")
        if result == -2:
            raise ValueError("active definition changed concurrently")
        return decision

    async def get(self, name: str, revision: int) -> WorkflowDefinition | None:
        value = await (await self._client()).get(self._definition_key(name, revision))
        return WorkflowDefinition.model_validate_json(value) if value else None

    async def active(self, name: str) -> WorkflowDefinition | None:
        redis = await self._client()
        pointer = await redis.get(f"{_ACTIVE_PREFIX}{name}")
        if not pointer:
            return None
        text = pointer.decode() if isinstance(pointer, bytes) else str(pointer)
        revision, _, _digest = text.partition(":")
        return await self.get(name, int(revision))

    async def decisions(self, name: str) -> tuple[PublicationDecision, ...]:
        values = await (await self._client()).lrange(f"{_DECISIONS_PREFIX}{name}", 0, -1)
        return tuple(PublicationDecision.model_validate_json(value) for value in values)

    @staticmethod
    def _definition_key(name: str, revision: int) -> str:
        return f"{_DEFINITION_PREFIX}{name}:{revision}"


class InMemoryDefinitionPublisher:
    """Deterministic publisher for local governance and contract tests."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}
        self._active: dict[str, WorkflowDefinition] = {}
        self._decisions: dict[str, list[PublicationDecision]] = {}

    async def publish(
        self,
        definition: WorkflowDefinition,
        *,
        actor: str,
        activate: bool = False,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        DeclarativeWorkflowCompiler(definition).validate()
        key = (definition.metadata.name, definition.metadata.revision)
        existing = self._definitions.get(key)
        if existing is not None and existing.digest != definition.digest:
            raise ValueError("published revision is immutable and has different content")
        previous = self._active.get(definition.metadata.name)
        if expected_active_digest and (
            previous is None or previous.digest != expected_active_digest
        ):
            raise ValueError("active definition changed concurrently")
        impact = compare_process_definitions(previous, definition) if previous else None
        if activate and impact and not impact.compatible_for_in_flight:
            raise ValueError("definition is incompatible with active workflow instances")
        self._definitions[key] = definition
        if activate:
            self._active[definition.metadata.name] = definition
        decision = PublicationDecision(
            workflow_name=definition.metadata.name,
            revision=definition.metadata.revision,
            digest=definition.digest,
            published_at=datetime.now(UTC),
            activated=activate,
            actor=actor,
            impact=impact.model_dump(mode="json") if impact else {},
        )
        self._decisions.setdefault(definition.metadata.name, []).append(decision)
        return decision

    async def get(self, name: str, revision: int) -> WorkflowDefinition | None:
        return self._definitions.get((name, revision))

    async def active(self, name: str) -> WorkflowDefinition | None:
        return self._active.get(name)

    async def decisions(self, name: str) -> tuple[PublicationDecision, ...]:
        return tuple(self._decisions.get(name, ()))
