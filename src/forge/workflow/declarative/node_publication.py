"""Project-scoped, immutable user-node publication."""

from __future__ import annotations

from typing import Any

from forge.orchestrator.checkpointer import get_redis_client
from forge.workflow.declarative.models import NodeDefinition
from forge.workflow.declarative.predicates import validate_predicate


def validate_node(definition: NodeDefinition) -> None:
    definition.validate_size()
    if definition.spec.type == "decision-v1":
        for rule in definition.spec.rules:
            for state in ("feature", "bug", "task_takeover"):
                validate_predicate(rule.when, state)


class NodePublisher:
    def __init__(self, project_key: str, redis_client: Any = None) -> None:
        if not project_key.strip():
            raise ValueError("project key is required")
        self.project_key = project_key.upper()
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    def _key(self, name: str, revision: int) -> str:
        return f"forge:node:def:{self.project_key}:{name}:{revision}"

    def _active_key(self, name: str) -> str:
        return f"forge:node:active:{self.project_key}:{name}"

    async def publish(self, definition: NodeDefinition) -> None:
        validate_node(definition)
        redis = await self._client()
        key = self._key(definition.metadata.name, definition.metadata.revision)
        value = definition.canonical_json()
        result = await redis.eval(
            """local x=redis.call('GET',KEYS[1])
            if x and x~=ARGV[1] then return -1 end
            local latest=redis.call('GET',KEYS[2])
            if not x and latest and tonumber(ARGV[2])<=tonumber(latest) then return -2 end
            if not x then
              redis.call('SET',KEYS[1],ARGV[1])
              redis.call('SET',KEYS[2],ARGV[2])
            end
            return 1""",
            2,
            key,
            f"forge:node:latest:{self.project_key}:{definition.metadata.name}",
            value,
            str(definition.metadata.revision),
        )
        if result == -1:
            raise ValueError("published node revision is immutable")
        if result == -2:
            raise ValueError("changed node content must increment metadata.revision")

    async def get(self, name: str, revision: int) -> NodeDefinition | None:
        value = await (await self._client()).get(self._key(name, revision))
        return NodeDefinition.model_validate_json(value) if value else None

    async def active(self, name: str) -> NodeDefinition | None:
        value = await (await self._client()).get(self._active_key(name))
        if not value:
            return None
        revision = int(value.decode() if isinstance(value, bytes) else value)
        return await self.get(name, revision)

    async def activate(
        self, name: str, revision: int, *, expected_digest: str | None = None
    ) -> NodeDefinition:
        target = await self.get(name, revision)
        if target is None:
            raise ValueError("node revision is unavailable")
        previous = await self.active(name)
        if previous and previous.digest != expected_digest:
            raise ValueError("active node changed concurrently")
        result = await (await self._client()).eval(
            """local old=redis.call('GET',KEYS[1])
            if ARGV[1]~='' and old~=ARGV[1] then return -1 end
            if ARGV[1]=='' and old then return -1 end
            redis.call('SET',KEYS[1],ARGV[2]); return 1""",
            1,
            self._active_key(name),
            str(previous.metadata.revision) if previous else "",
            str(revision),
        )
        if result == -1:
            raise ValueError("active node changed concurrently")
        return target

    async def rollback(self, name: str, revision: int, *, expected_digest: str) -> NodeDefinition:
        previous = await self.active(name)
        if previous is None or revision >= previous.metadata.revision:
            raise ValueError("rollback requires an older revision")
        return await self.activate(name, revision, expected_digest=expected_digest)

    async def history(self, name: str) -> tuple[NodeDefinition, ...]:
        redis = await self._client()
        keys = []
        cursor = 0
        while True:
            cursor, found = await redis.scan(cursor=cursor, match=self._key(name, "*"))
            keys.extend(found)
            if cursor == 0:
                break
        result = [
            NodeDefinition.model_validate_json(value)
            for key in keys
            if (value := await redis.get(key))
        ]
        return tuple(sorted(result, key=lambda item: item.metadata.revision))

    async def list_nodes(self) -> tuple[str, ...]:
        redis = await self._client()
        names = set()
        cursor = 0
        while True:
            cursor, found = await redis.scan(
                cursor=cursor, match=f"forge:node:def:{self.project_key}:*"
            )
            for key in found:
                names.add((key.decode() if isinstance(key, bytes) else key).rsplit(":", 2)[-2])
            if cursor == 0:
                break
        return tuple(sorted(names))


class InMemoryNodePublisher:
    def __init__(self, project_key: str = "DEFAULT") -> None:
        self.project_key = project_key.upper()
        self._definitions: dict[tuple[str, int], NodeDefinition] = {}
        self._active: dict[str, NodeDefinition] = {}

    async def publish(self, definition: NodeDefinition) -> None:
        validate_node(definition)
        key = (definition.metadata.name, definition.metadata.revision)
        if key in self._definitions and self._definitions[key].digest != definition.digest:
            raise ValueError("published node revision is immutable")
        if key not in self._definitions and any(
            item == definition.metadata.name and rev >= definition.metadata.revision
            for item, rev in self._definitions
        ):
            raise ValueError("changed node content must increment metadata.revision")
        self._definitions[key] = definition

    async def get(self, name: str, revision: int) -> NodeDefinition | None:
        return self._definitions.get((name, revision))

    async def active(self, name: str) -> NodeDefinition | None:
        return self._active.get(name)

    async def activate(
        self, name: str, revision: int, *, expected_digest: str | None = None
    ) -> NodeDefinition:
        target = await self.get(name, revision)
        if target is None:
            raise ValueError("node revision is unavailable")
        previous = self._active.get(name)
        if previous and previous.digest != expected_digest:
            raise ValueError("active node changed concurrently")
        self._active[name] = target
        return target

    async def rollback(self, name: str, revision: int, *, expected_digest: str) -> NodeDefinition:
        previous = self._active.get(name)
        if previous is None or revision >= previous.metadata.revision:
            raise ValueError("rollback requires an older revision")
        return await self.activate(name, revision, expected_digest=expected_digest)

    async def history(self, name: str) -> tuple[NodeDefinition, ...]:
        return tuple(
            sorted(
                (value for (item, _), value in self._definitions.items() if item == name),
                key=lambda value: value.metadata.revision,
            )
        )

    async def list_nodes(self) -> tuple[str, ...]:
        return tuple(sorted({name for name, _ in self._definitions}))
