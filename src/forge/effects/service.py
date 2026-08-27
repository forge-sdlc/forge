"""Effect submission, execution and recovery service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.effects.journal import EffectJournal
from forge.effects.models import EffectRecord, EffectRecordStatus
from forge.integrations.source_control.errors import ConflictError, TransientProviderError
from forge.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)


class RequiredEffectError(RuntimeError):
    def __init__(self, record: EffectRecord) -> None:
        super().__init__(f"Required effect {record.command.effect_id} is {record.status.value}")
        self.record = record


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

    async def execute_now(self, command: EffectCommand) -> EffectRecord:
        """Persist and exclusively execute one workflow-critical effect."""
        submitted = await self.journal.submit(command)
        if submitted.result is not None and submitted.status.value not in {
            "pending",
            "running",
            "retryable_failure",
        }:
            return submitted
        claimed = await self.journal.claim(command.idempotency_key)
        if claimed is None:
            current = await self.journal.get(command.idempotency_key)
            if current is None:  # pragma: no cover - journal contract violation
                raise RuntimeError("submitted effect disappeared from journal")
            return current
        return await self._execute(claimed)

    async def execute_required(self, command: EffectCommand) -> EffectRecord:
        """Execute now and fail closed until the durable effect succeeds."""
        record = await self.execute_now(command)
        if record.status is not EffectRecordStatus.SUCCEEDED:
            raise RequiredEffectError(record)
        return record

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
            status = self._failure_status(exc, record.attempt)
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

    def _failure_status(self, exc: Exception, attempt: int) -> EffectResultStatus:
        if isinstance(exc, (ConflictError, ValueError, KeyError)):
            return EffectResultStatus.PRECONDITION_FAILED
        if isinstance(exc, TransientProviderError) and attempt < self.max_attempts:
            return EffectResultStatus.RETRYABLE_FAILURE
        return (
            EffectResultStatus.TERMINAL_FAILURE
            if attempt >= self.max_attempts
            else EffectResultStatus.RETRYABLE_FAILURE
        )

    async def replay(self, idempotency_key: str) -> EffectRecord:
        """Explicitly reschedule a terminal effect while retaining its history."""
        return await self.journal.replay(idempotency_key)

    async def purge_terminal_before(self, cutoff: datetime) -> int:
        """Apply the operator-selected terminal-record retention cutoff."""
        return await self.journal.purge_terminal_before(cutoff)

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
