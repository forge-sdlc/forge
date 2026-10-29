"""Ticket ownership propagated to durable writes through async task context.

Redis WATCH includes key expiry. Checking the token inside WATCH/MULTI prevents
an expired worker from writing after another worker acquires the ticket. This
adapter targets the single Redis deployment used by Forge, not Redis Cluster.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import WatchError

from forge.orchestrator.ownership_metrics import STALE_WRITES


class TicketOwnershipLost(RuntimeError):
    """The current task no longer owns its ticket's durable write authority."""

    def __init__(self, message: str) -> None:
        STALE_WRITES.inc()
        super().__init__(message)


@dataclass(frozen=True)
class TicketOwnership:
    redis: Any
    key: str
    token: bytes | str


_current: ContextVar[TicketOwnership | None] = ContextVar("ticket_ownership", default=None)


def get_ticket_ownership() -> TicketOwnership | None:
    return _current.get()


@contextmanager
def ownership_scope(owner: TicketOwnership) -> Iterator[None]:
    context_token = _current.set(owner)
    try:
        yield
    finally:
        _current.reset(context_token)


def _token(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


async def assert_ticket_ownership() -> None:
    owner = get_ticket_ownership()
    if owner is not None and _token(await owner.redis.get(owner.key)) != _token(owner.token):
        raise TicketOwnershipLost(f"Ticket ownership lost: {owner.key}")


@asynccontextmanager
async def owned_transaction(redis: Any) -> AsyncIterator[Any]:
    """Yield a WATCHed pipeline; caller adds watches, calls multi, then executes.

    Callers retry WatchError by reopening the context. An expired/replaced owner
    raises TicketOwnershipLost on the next attempt. Without a ticket context,
    operator and recovery services retain their own normal ownership protocol.
    """
    async with redis.pipeline(transaction=True) as pipeline:
        owner = get_ticket_ownership()
        if owner is not None:
            await pipeline.watch(owner.key)
            if _token(await pipeline.get(owner.key)) != _token(owner.token):
                raise TicketOwnershipLost(f"Ticket ownership lost: {owner.key}")
        yield pipeline


class OwnedPipeline(Pipeline):
    """Fence the saver/library's queued commands, including RedisJSON writes."""

    async def execute(self, raise_on_error: bool = True) -> list[Any]:
        owner = get_ticket_ownership()
        if owner is None or not self.command_stack:
            return await super().execute(raise_on_error=raise_on_error)
        # Library pipelines do not WATCH. Refuse to silently drop a caller's
        # existing optimistic transaction if that ever changes upstream.
        if self.watching:
            raise RuntimeError("OwnedPipeline cannot replace an existing WATCH transaction")
        commands = list(self.command_stack)
        scripts = set(self.scripts)
        await self.reset()  # type: ignore[no-untyped-call]
        while True:
            try:
                await self.watch(owner.key)
                if _token(await self.get(owner.key)) != _token(owner.token):
                    raise TicketOwnershipLost(f"Ticket ownership lost: {owner.key}")
                self.multi()  # type: ignore[no-untyped-call]
                self.command_stack = list(commands)
                self.scripts = set(scripts)
                return await super().execute(raise_on_error=raise_on_error)
            except WatchError:
                # A renewal can change the lock while preserving its owner.
                # Recheck the token; never replay after authority has changed.
                continue
            finally:
                await self.reset()  # type: ignore[no-untyped-call]


class OwnedRedis(Redis):
    """Redis client passed to the saver and its RedisVL indexes.

    Both direct commands and library-created pipelines are guarded. This keeps
    checkpoint JSON, pending writes, indexes, and latest pointers behind the same
    atomic token check instead of a racy check before calling the saver.
    """

    def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> OwnedPipeline:
        return OwnedPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint)

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        if get_ticket_ownership() is None:
            return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]
        async with self.pipeline() as pipeline:
            pipeline.execute_command(*args, **options)
            return (await pipeline.execute())[0]
