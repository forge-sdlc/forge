"""Immutable process-definition publication and explicit rollout decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from forge.domain import DomainModel
from forge.orchestrator.checkpointer import get_redis_client
from forge.workflow.declarative.compiler import DeclarativeWorkflowCompiler
from forge.workflow.declarative.manifest import ProcessChangeImpact, compare_process_definitions
from forge.workflow.declarative.models import WorkflowDefinition

_DEFINITION_PREFIX = "forge:process:def:"
_ACTIVE_PREFIX = "forge:process:active:"
_DECISIONS_PREFIX = "forge:process:decisions:"
_LATEST_PREFIX = "forge:process:latest:"

# Return values: 1 new publication, 0 idempotent publication, -1 immutable
# collision, -2 revision went backwards for changed content.
_PUBLISH_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing and existing ~= ARGV[1] then return -1 end
local latest = redis.call('GET', KEYS[2])
if not existing and latest then
  local separator = string.find(latest, ':')
  local latest_revision = tonumber(string.sub(latest, 1, separator - 1))
  local latest_digest = string.sub(latest, separator + 1)
  if ARGV[3] ~= latest_digest and tonumber(ARGV[2]) <= latest_revision then return -2 end
end
if not existing then
  redis.call('SET', KEYS[1], ARGV[1])
  if not latest or tonumber(ARGV[2]) > tonumber(string.sub(latest, 1, string.find(latest, ':') - 1)) then
    redis.call('SET', KEYS[2], ARGV[2] .. ':' .. ARGV[3])
  end
end
redis.call('RPUSH', KEYS[3], ARGV[4])
return existing and 0 or 1
"""

_ACTIVATE_SCRIPT = """
local target = redis.call('GET', KEYS[1])
if not target then return -1 end
local active = redis.call('GET', KEYS[2])
if ARGV[1] ~= '' and (not active or string.sub(active, string.find(active, ':') + 1) ~= ARGV[1]) then return -2 end
redis.call('SET', KEYS[2], ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[3])
return 1
"""


class PublicationDecision(DomainModel):
    """An immutable audit entry for publishing or changing activation."""

    workflow_name: str
    revision: int
    digest: str
    published_at: datetime
    activated: bool = False
    actor: str
    impact: dict[str, Any] = Field(default_factory=dict)
    project_key: str = ""
    action: Literal["publish", "activate", "rollback"] = "publish"
    reason: str = ""


def _compatible_impact(impact: ProcessChangeImpact, *, rollback: bool = False) -> bool:
    """Fail closed for activation; rollback only relaxes revision direction."""
    if rollback:
        return not impact.state_profile_changed and not impact.missing_resume_mappings
    return impact.compatible_for_in_flight


class DefinitionPublisher:
    """Project-scoped immutable definition store and rollout decision log."""

    def __init__(self, project_key: str, redis_client: Any = None) -> None:
        if not project_key or not project_key.strip():
            raise ValueError("project_key is required for governed publication")
        self.project_key = project_key.upper()
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def publish(
        self, definition: WorkflowDefinition, *, actor: str, reason: str, activate: bool = False
    ) -> PublicationDecision:
        """Validate and persist an immutable artifact, without activating it."""
        if activate:
            raise ValueError("publication and activation are separate decisions; use activate()")
        self._validate(definition)
        decision = self._decision(definition, actor=actor, reason=reason, action="publish")
        result = await (await self._client()).eval(
            _PUBLISH_SCRIPT,
            3,
            self._definition_key(definition.metadata.name, definition.metadata.revision),
            self._latest_key(definition.metadata.name),
            self._decisions_key(definition.metadata.name),
            definition.canonical_json(),
            str(definition.metadata.revision),
            definition.digest,
            decision.model_dump_json(),
        )
        if result == -1:
            raise ValueError("published revision is immutable and has different content")
        if result == -2:
            raise ValueError("changed workflow content must increment metadata.revision")
        return decision

    async def activate(
        self,
        name: str | WorkflowDefinition,
        revision: int | None = None,
        *,
        actor: str,
        reason: str,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        """Activate an existing artifact using compare-and-set semantics."""
        name, revision = self._target_identity(name, revision)
        return await self._set_active(
            name,
            revision,
            actor=actor,
            reason=reason,
            action="activate",
            expected_active_digest=expected_active_digest,
        )

    async def rollback(
        self,
        name: str | WorkflowDefinition,
        revision: int | None = None,
        *,
        actor: str,
        reason: str,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        """Move activation to an older compatible artifact; never mutate history."""
        name, revision = self._target_identity(name, revision)
        current = await self.active(name)
        if current is None or revision >= current.metadata.revision:
            raise ValueError("rollback target must be an already-published older revision")
        return await self._set_active(
            name,
            revision,
            actor=actor,
            reason=reason,
            action="rollback",
            expected_active_digest=expected_active_digest,
        )

    async def _set_active(
        self,
        name: str,
        revision: int,
        *,
        actor: str,
        reason: str,
        action: Literal["activate", "rollback"],
        expected_active_digest: str | None,
    ) -> PublicationDecision:
        target = await self.get(name, revision)
        if target is None:
            raise ValueError(f"published workflow '{name}' revision {revision} is unavailable")
        if target.metadata.name != name:
            raise ValueError("published workflow name does not match activation key")
        self._validate(target)
        previous = await self.active(name)
        if previous is not None and expected_active_digest is None:
            raise ValueError(
                "expected_active_digest is required when replacing an active definition"
            )
        if expected_active_digest and (
            previous is None or previous.digest != expected_active_digest
        ):
            raise ValueError("active definition changed concurrently")
        impact = compare_process_definitions(previous, target) if previous else None
        if impact is not None and not _compatible_impact(impact, rollback=action == "rollback"):
            raise ValueError("definition is incompatible with active workflow instances")
        decision = self._decision(
            target, actor=actor, reason=reason, action=action, activated=True, impact=impact
        )
        result = await (await self._client()).eval(
            _ACTIVATE_SCRIPT,
            3,
            self._definition_key(name, revision),
            self._active_key(name),
            self._decisions_key(name),
            expected_active_digest or "",
            self._pointer(target),
            decision.model_dump_json(),
        )
        if result == -1:
            raise ValueError(f"published workflow '{name}' revision {revision} is unavailable")
        if result == -2:
            raise ValueError("active definition changed concurrently")
        return decision

    async def get(self, name: str, revision: int) -> WorkflowDefinition | None:
        value = await (await self._client()).get(self._definition_key(name, revision))
        return WorkflowDefinition.model_validate_json(value) if value else None

    async def active(self, name: str) -> WorkflowDefinition | None:
        pointer = await (await self._client()).get(self._active_key(name))
        if not pointer:
            return None
        text = pointer.decode() if isinstance(pointer, bytes) else str(pointer)
        revision, _, _digest = text.partition(":")
        return await self.get(name, int(revision))

    async def decisions(self, name: str) -> tuple[PublicationDecision, ...]:
        values = await (await self._client()).lrange(self._decisions_key(name), 0, -1)
        return tuple(PublicationDecision.model_validate_json(value) for value in values)

    async def history(self, name: str) -> tuple[WorkflowDefinition, ...]:
        redis = await self._client()
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, found = await redis.scan(cursor=cursor, match=self._definition_key(name, "*"))
            keys.extend(found)
            if cursor == 0:
                break
        definitions = []
        for key in keys:
            value = await redis.get(key)
            if value:
                definitions.append(WorkflowDefinition.model_validate_json(value))
        return tuple(sorted(definitions, key=lambda item: item.metadata.revision))

    async def list_workflows(self) -> tuple[str, ...]:
        redis = await self._client()
        names: set[str] = set()
        cursor = 0
        prefix = f"{_DEFINITION_PREFIX}{self.project_key}:"
        while True:
            cursor, found = await redis.scan(cursor=cursor, match=f"{prefix}*")
            for key in found:
                text = key.decode() if isinstance(key, bytes) else str(key)
                remainder = text[len(prefix) :]
                if ":" in remainder:
                    names.add(remainder.rsplit(":", 1)[0])
            if cursor == 0:
                break
        return tuple(sorted(names))

    def _validate(self, definition: WorkflowDefinition) -> None:
        definition.validate_property_size()
        DeclarativeWorkflowCompiler(definition).validate_for_publication()

    def _decision(
        self,
        definition: WorkflowDefinition,
        *,
        actor: str,
        reason: str,
        action: Literal["publish", "activate", "rollback"],
        activated: bool = False,
        impact: ProcessChangeImpact | None = None,
    ) -> PublicationDecision:
        if not actor.strip():
            raise ValueError("actor is required for governed decisions")
        if not reason.strip():
            raise ValueError("reason is required for governed decisions")
        return PublicationDecision(
            project_key=self.project_key,
            workflow_name=definition.metadata.name,
            revision=definition.metadata.revision,
            digest=definition.digest,
            published_at=datetime.now(UTC),
            activated=activated,
            actor=actor,
            reason=reason,
            action=action,
            impact=impact.model_dump(mode="json") if impact else {},
        )

    @staticmethod
    def _target_identity(name: str | WorkflowDefinition, revision: int | None) -> tuple[str, int]:
        if isinstance(name, WorkflowDefinition):
            if revision is not None and revision != name.metadata.revision:
                raise ValueError("activation revision does not match definition")
            return name.metadata.name, name.metadata.revision
        if revision is None:
            raise ValueError("activation revision is required")
        return name, revision

    def _prefix(self, prefix: str, name: str) -> str:
        return f"{prefix}{self.project_key}:{name}"

    def _definition_key(self, name: str, revision: int | str) -> str:
        return f"{self._prefix(_DEFINITION_PREFIX, name)}:{revision}"

    def _latest_key(self, name: str) -> str:
        return self._prefix(_LATEST_PREFIX, name)

    def _active_key(self, name: str) -> str:
        return self._prefix(_ACTIVE_PREFIX, name)

    def _decisions_key(self, name: str) -> str:
        return self._prefix(_DECISIONS_PREFIX, name)

    @staticmethod
    def _pointer(definition: WorkflowDefinition) -> str:
        return f"{definition.metadata.revision}:{definition.digest}"


class InMemoryDefinitionPublisher:
    """Deterministic project-scoped publisher for local and contract tests."""

    def __init__(self, project_key: str = "DEFAULT") -> None:
        if not project_key or not project_key.strip():
            raise ValueError("project_key is required for governed publication")
        self.project_key = project_key.upper()
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}
        self._active: dict[str, WorkflowDefinition] = {}
        self._decisions: dict[str, list[PublicationDecision]] = {}

    async def publish(
        self, definition: WorkflowDefinition, *, actor: str, reason: str, activate: bool = False
    ) -> PublicationDecision:
        if activate:
            raise ValueError("publication and activation are separate decisions; use activate()")
        self._validate(definition)
        key = (definition.metadata.name, definition.metadata.revision)
        existing = self._definitions.get(key)
        if existing is not None and existing.digest != definition.digest:
            raise ValueError("published revision is immutable and has different content")
        published = await self.history(definition.metadata.name)
        if any(
            item.digest != definition.digest
            and item.metadata.revision >= definition.metadata.revision
            for item in published
        ):
            raise ValueError("changed workflow content must increment metadata.revision")
        self._definitions[key] = definition
        decision = self._decision(definition, actor=actor, reason=reason, action="publish")
        self._decisions.setdefault(definition.metadata.name, []).append(decision)
        return decision

    async def activate(
        self,
        name: str | WorkflowDefinition,
        revision: int | None = None,
        *,
        actor: str,
        reason: str,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        name, revision = self._target_identity(name, revision)
        return await self._set_active(
            name,
            revision,
            actor=actor,
            reason=reason,
            action="activate",
            expected_active_digest=expected_active_digest,
        )

    async def rollback(
        self,
        name: str | WorkflowDefinition,
        revision: int | None = None,
        *,
        actor: str,
        reason: str,
        expected_active_digest: str | None = None,
    ) -> PublicationDecision:
        name, revision = self._target_identity(name, revision)
        current = self._active.get(name)
        if current is None or revision >= current.metadata.revision:
            raise ValueError("rollback target must be an already-published older revision")
        return await self._set_active(
            name,
            revision,
            actor=actor,
            reason=reason,
            action="rollback",
            expected_active_digest=expected_active_digest,
        )

    async def _set_active(
        self,
        name: str,
        revision: int,
        *,
        actor: str,
        reason: str,
        action: Literal["activate", "rollback"],
        expected_active_digest: str | None,
    ) -> PublicationDecision:
        target = self._definitions.get((name, revision))
        if target is None:
            raise ValueError(f"published workflow '{name}' revision {revision} is unavailable")
        if target.metadata.name != name:
            raise ValueError("published workflow name does not match activation key")
        current = self._active.get(name)
        if current is not None and expected_active_digest is None:
            raise ValueError(
                "expected_active_digest is required when replacing an active definition"
            )
        if expected_active_digest and (current is None or current.digest != expected_active_digest):
            raise ValueError("active definition changed concurrently")
        impact = compare_process_definitions(current, target) if current else None
        if impact and not _compatible_impact(impact, rollback=action == "rollback"):
            raise ValueError("definition is incompatible with active workflow instances")
        self._active[name] = target
        decision = self._decision(
            target, actor=actor, reason=reason, action=action, activated=True, impact=impact
        )
        self._decisions.setdefault(name, []).append(decision)
        return decision

    async def get(self, name: str, revision: int) -> WorkflowDefinition | None:
        return self._definitions.get((name, revision))

    async def active(self, name: str) -> WorkflowDefinition | None:
        return self._active.get(name)

    async def decisions(self, name: str) -> tuple[PublicationDecision, ...]:
        return tuple(self._decisions.get(name, ()))

    async def history(self, name: str) -> tuple[WorkflowDefinition, ...]:
        return tuple(
            sorted(
                (definition for (item, _), definition in self._definitions.items() if item == name),
                key=lambda item: item.metadata.revision,
            )
        )

    async def list_workflows(self) -> tuple[str, ...]:
        return tuple(sorted({name for name, _ in self._definitions}))

    def _validate(self, definition: WorkflowDefinition) -> None:
        definition.validate_property_size()
        DeclarativeWorkflowCompiler(definition).validate_for_publication()

    def _decision(
        self,
        definition: WorkflowDefinition,
        *,
        actor: str,
        reason: str,
        action: Literal["publish", "activate", "rollback"],
        activated: bool = False,
        impact: ProcessChangeImpact | None = None,
    ) -> PublicationDecision:
        if not actor.strip():
            raise ValueError("actor is required for governed decisions")
        if not reason.strip():
            raise ValueError("reason is required for governed decisions")
        return PublicationDecision(
            project_key=self.project_key,
            workflow_name=definition.metadata.name,
            revision=definition.metadata.revision,
            digest=definition.digest,
            published_at=datetime.now(UTC),
            activated=activated,
            actor=actor,
            reason=reason,
            action=action,
            impact=impact.model_dump(mode="json") if impact else {},
        )

    @staticmethod
    def _target_identity(name: str | WorkflowDefinition, revision: int | None) -> tuple[str, int]:
        if isinstance(name, WorkflowDefinition):
            if revision is not None and revision != name.metadata.revision:
                raise ValueError("activation revision does not match definition")
            return name.metadata.name, name.metadata.revision
        if revision is None:
            raise ValueError("activation revision is required")
        return name, revision
