"""Provider executor contracts and operation registry."""

from __future__ import annotations

from typing import Protocol

from forge.domain import EffectCommand, EffectResult


class EffectExecutor(Protocol):
    operation: str

    async def execute(self, command: EffectCommand) -> EffectResult: ...


class EffectExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, EffectExecutor] = {}

    def register(self, executor: EffectExecutor) -> None:
        if executor.operation in self._executors:
            raise ValueError(f"Executor already registered for {executor.operation}")
        self._executors[executor.operation] = executor

    def resolve(self, operation: str) -> EffectExecutor:
        try:
            return self._executors[operation]
        except KeyError as exc:
            raise ValueError(f"No effect executor registered for {operation}") from exc
