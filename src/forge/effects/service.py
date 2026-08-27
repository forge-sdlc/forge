"""Effect submission, execution and recovery service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.effects.journal import EffectJournal
from forge.effects.models import EffectRecord
from forge.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)


class EffectService:
    def __init__(
        self,
        journal: EffectJournal,
        executors: EffectExecutorRegistry,
        *,
        max_attempts: int = 3,
        base_retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        self.journal = journal
        self.executors = executors
        self.max_attempts = max_attempts
        self.base_retry_delay = base_retry_delay

    async def submit(self, command: EffectCommand) -> EffectRecord:
        """Persist intent before any provider call; duplicates return the first record."""
        return await self.journal.submit(command)

    async def run_due(self, limit: int = 10) -> list[EffectRecord]:
        completed = []
        for record in await self.journal.claim_due(limit):
            completed.append(await self._execute(record))
        return completed

    async def _execute(self, record: EffectRecord) -> EffectRecord:
        command = record.command
        try:
            executor = self.executors.resolve(command.operation)
            result = await executor.execute(command)
        except Exception as exc:
            status = (
                EffectResultStatus.TERMINAL_FAILURE
                if record.attempt >= self.max_attempts
                else EffectResultStatus.RETRYABLE_FAILURE
            )
            result = EffectResult(
                effect_id=command.effect_id,
                idempotency_key=command.idempotency_key,
                status=status,
                completed_at=datetime.now(UTC),
                error_code=type(exc).__name__,
                error_message=redact_secrets(str(exc))[:1000],
            )

        if (
            result.status is EffectResultStatus.RETRYABLE_FAILURE
            and record.attempt < self.max_attempts
        ):
            delay = self.base_retry_delay * (2 ** (record.attempt - 1))
            return await self.journal.retry(result, delay)
        if result.status is EffectResultStatus.RETRYABLE_FAILURE:
            result = result.model_copy(update={"status": EffectResultStatus.TERMINAL_FAILURE})
        return await self.journal.complete(result)

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        interval: float = 5.0,
        limit: int = 10,
    ) -> None:
        while not stop.is_set():
            try:
                await self.run_due(limit)
            except Exception:
                logger.exception("Durable effect sweep failed; retrying on the next interval")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
