"""Effect submission, execution and recovery service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from forge.api.routes.metrics import (
    record_effect_attempt,
    record_effect_replay,
    record_effect_result,
)
from forge.domain import EffectCommand, EffectResult, EffectResultStatus
from forge.effects.executors import EffectExecutorRegistry
from forge.effects.journal import EffectClaimLostError, EffectJournal
from forge.effects.models import EffectRecord, EffectRecordStatus
from forge.integrations.source_control.errors import ConflictError, RateLimitedError
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
        required_effect_wait_timeout: timedelta = timedelta(seconds=30),
        required_effect_poll_interval: float = 0.05,
        lease_renewal_interval: float | None = None,
        lease_renewal_timeout: float = 5.0,
    ) -> None:
        self.journal = journal
        self.executors = executors
        self.max_attempts = max_attempts
        self.base_retry_delay = base_retry_delay
        self.required_effect_wait_timeout = required_effect_wait_timeout
        self.required_effect_poll_interval = required_effect_poll_interval
        lease_seconds = journal.lease_duration.total_seconds()
        self.lease_renewal_interval = lease_renewal_interval or max(0.01, lease_seconds / 3)
        self.lease_renewal_timeout = max(
            0.001, min(lease_renewal_timeout, self.lease_renewal_interval)
        )

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
        """Execute a critical effect, waiting for a concurrent owner if necessary.

        A workflow invocation and the background recovery sweep may race to
        claim the same newly-submitted effect.  Claim ownership is exclusive,
        but ownership by the sweep is not a failure: it will complete the same
        idempotent provider mutation.  Wait for that owner to settle rather
        than failing a workflow on the transient ``pending``/``running``
        observation.  Terminal and retryable failures still fail closed.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.required_effect_wait_timeout.total_seconds()
        while True:
            record = await self.execute_now(command)
            if record.status is EffectRecordStatus.SUCCEEDED:
                return record
            if record.status not in {EffectRecordStatus.PENDING, EffectRecordStatus.RUNNING}:
                raise RequiredEffectError(record)
            if loop.time() >= deadline:
                raise RequiredEffectError(record)
            await asyncio.sleep(self.required_effect_poll_interval)

    async def run_due(self, limit: int = 10) -> list[EffectRecord]:
        completed = []
        # Claim only the effect about to start. A sequential sweep must not
        # leave later records waiting under expiring leases.
        for _ in range(limit):
            claimed = await self.journal.claim_due(1)
            if not claimed:
                break
            record = claimed[0]
            completed.append(await self._execute(record))
        return completed

    async def _execute(self, record: EffectRecord) -> EffectRecord:
        command = record.command
        if record.claim_token is None:
            raise EffectClaimLostError(
                f"Effect {command.idempotency_key} has no current claim token"
            )
        record_effect_attempt(command.operation)
        try:
            executor = self.executors.resolve(command.operation)
            retry_safe = bool(getattr(executor, "retry_safe_after_ambiguous_failure", False))
            if record.recovered_after_expiry and not retry_safe:
                result = EffectResult(
                    effect_id=command.effect_id,
                    idempotency_key=command.idempotency_key,
                    status=EffectResultStatus.INDETERMINATE,
                    completed_at=datetime.now(UTC),
                    error_code="ExpiredEffectClaim",
                    error_message=(
                        "A prior effect attempt lost its lease before recording an outcome; "
                        "provider state requires reconciliation before another write."
                    ),
                )
            else:
                result = await self._execute_with_lease(record, executor)
        except EffectClaimLostError:
            # A later owner may already be running the effect. Never write the
            # stale result; return the current durable view to the caller.
            current = await self.journal.get(command.idempotency_key)
            return current or record
        except Exception as exc:
            executor = self.executors.maybe_resolve(command.operation)
            retry_safe = bool(
                executor is not None
                and getattr(executor, "retry_safe_after_ambiguous_failure", False)
            )
            status = self._failure_status(exc, record.attempt, retry_safe=retry_safe)
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
            try:
                retried = await self.journal.retry(result, delay, record.claim_token)
            except EffectClaimLostError:
                return await self.journal.get(command.idempotency_key) or record
            record_effect_result(command.operation, retried.status.value)
            return retried
        if result.status is EffectResultStatus.RETRYABLE_FAILURE:
            result = result.model_copy(update={"status": EffectResultStatus.TERMINAL_FAILURE})
        try:
            completed = await self.journal.complete(result, record.claim_token)
        except EffectClaimLostError:
            return await self.journal.get(command.idempotency_key) or record
        record_effect_result(command.operation, completed.status.value)
        return completed

    async def _execute_with_lease(self, record: EffectRecord, executor) -> EffectResult:
        execution = asyncio.create_task(executor.execute(record.command))
        keeper = asyncio.create_task(self._renew_until_done(record))
        try:
            done, _ = await asyncio.wait({execution, keeper}, return_when=asyncio.FIRST_COMPLETED)
            if keeper in done:
                # Renewal only completes on ownership loss or a transport error.
                await keeper
                raise EffectClaimLostError(record.command.idempotency_key)
            return await execution
        finally:
            if not execution.done():
                execution.cancel()
            if not keeper.done():
                keeper.cancel()
            await asyncio.gather(execution, keeper, return_exceptions=True)

    async def _renew_until_done(self, record: EffectRecord) -> None:
        assert record.claim_token is not None
        while True:
            try:
                await asyncio.sleep(self.lease_renewal_interval)
                renewed = await asyncio.wait_for(
                    self.journal.renew(record.command.idempotency_key, record.claim_token),
                    timeout=self.lease_renewal_timeout,
                )
            except TimeoutError as exc:
                raise EffectClaimLostError(
                    f"Timed out renewing effect {record.command.idempotency_key}"
                ) from exc
            except Exception as exc:
                raise EffectClaimLostError(
                    f"Failed to renew effect {record.command.idempotency_key}"
                ) from exc
            if renewed is None:
                raise EffectClaimLostError(record.command.idempotency_key)

    def _failure_status(
        self, exc: Exception, attempt: int, *, retry_safe: bool
    ) -> EffectResultStatus:
        if isinstance(exc, (ConflictError, ValueError, KeyError)):
            return EffectResultStatus.PRECONDITION_FAILED
        if isinstance(exc, RateLimitedError):
            return EffectResultStatus.RETRYABLE_FAILURE
        if not retry_safe:
            return EffectResultStatus.INDETERMINATE
        return (
            EffectResultStatus.TERMINAL_FAILURE
            if attempt >= self.max_attempts
            else EffectResultStatus.RETRYABLE_FAILURE
        )

    async def replay(self, idempotency_key: str) -> EffectRecord:
        """Explicitly reschedule a terminal effect while retaining its history."""
        replayed = await self.journal.replay(idempotency_key)
        record_effect_replay(replayed.command.operation)
        return replayed

    async def reconcile(
        self,
        idempotency_key: str,
        *,
        applied: bool,
        note: str,
        provider_reference: str | None = None,
    ) -> EffectRecord:
        """Resolve an ambiguous provider outcome using an explicit operator fact."""
        return await self.journal.reconcile(
            idempotency_key,
            applied=applied,
            note=note,
            provider_reference=provider_reference,
        )

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
