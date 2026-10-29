"""Observation ledger with source-independent deduplication and monotonic revisions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.exceptions import WatchError

from forge.domain import Observation, stable_identity
from forge.orchestrator.checkpointer import get_redis_client
from forge.orchestrator.ownership import owned_transaction
from forge.reconciliation.metrics import refresh_processing_metrics
from forge.reconciliation.models import (
    DriftClass,
    ObservationDecision,
    ObservationDisposition,
    ObservationProcessingRecord,
    ProcessingPhase,
    ReconciledResource,
)

PROTECTED_WORKFLOW_FACTS = {
    # Execution position and immutable process identity.
    "current_node",
    "workflow_name",
    "workflow_revision",
    "workflow_digest",
    "workflow_definition_revision",
    "workflow_definition_digest",
    "workflow_definition",
    "workflow_pin_status",
    "workflow_state_profile",
    "workflow_position",
    "workflow_transition_count",
    "workflow_node_attempts",
    "command_decisions",
    "command_handoffs",
    # Checkpoint control fields.  External providers may report a status, but
    # they cannot directly pause, block, retry, or move a workflow checkpoint.
    "is_paused",
    "is_blocked",
    "retry_count",
    "last_error",
    "node_outcome",
    "pending_effects",
    "effect_journal",
}
_RESOURCE_PREFIX = "forge:observations:resource:"
_DELIVERY_PREFIX = "forge:observations:delivery:"
_HISTORY_PREFIX = "forge:observations:history:"
_RUN_HISTORY_PREFIX = "forge:observations:run:"
_PROCESSING_PREFIX = "forge:observations:processing:"
_PROCESSING_PENDING = "forge:observations:processing:pending"
_PROCESSING_HISTORY = "forge:observations:processing:history"
# Blocked decisions remain ordering barriers until an operator resolves them.
_UNFINISHED_PROCESSING_PHASES = {
    ProcessingPhase.PENDING,
    ProcessingPhase.RUNNING,
    ProcessingPhase.PREPARED,
    ProcessingPhase.BLOCKED,
}


class ObservationLedger(Protocol):
    async def record(self, observation: Observation) -> ObservationDecision: ...

    async def accept(
        self, observation: Observation, ticket_key: str, ingress_message: dict[str, Any]
    ) -> tuple[ObservationDecision, ObservationProcessingRecord | None]: ...

    async def get_processing(
        self, delivery_identity: str
    ) -> ObservationProcessingRecord | None: ...

    async def save_processing(self, record: ObservationProcessingRecord) -> None: ...

    async def pending_processing(
        self, *, ticket_key: str | None = None, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]:
        """Return unfinished observations, including blocked ordering barriers."""
        ...

    async def processing_for_ticket(
        self, ticket_key: str, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]: ...

    async def latest(self, observation: Observation) -> ReconciledResource | None: ...

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]: ...

    async def history_for_run(self, run_id: str) -> Sequence[ObservationDecision]: ...


def observation_run_id(observation: Observation) -> str | None:
    """Extract workflow correlation without coupling the ledger to Jira."""
    for value in (
        observation.correlation.get("workflow_ticket_key"),
        observation.correlation.get("ticket_key"),
    ):
        if isinstance(value, str) and value:
            return value
    issue = observation.facts.get("issue")
    if isinstance(issue, dict) and isinstance(issue.get("key"), str):
        return issue["key"]
    return None


def resource_identity(observation: Observation) -> str:
    return stable_identity(
        "observed-resource",
        {
            "source_system": observation.source_system,
            "resource_type": observation.resource.resource_type,
            "external_id": observation.resource.external_id,
            "namespace": observation.resource.namespace,
        },
    )


class InMemoryObservationLedger:
    """Reference implementation used by ingress conformance fixtures."""

    def __init__(self) -> None:
        self._resources: dict[str, ReconciledResource] = {}
        self._history: dict[str, list[ObservationDecision]] = {}
        self._deliveries: dict[str, ObservationDecision] = {}
        self._processing: dict[str, ObservationProcessingRecord] = {}
        self._run_history: dict[str, list[ObservationDecision]] = {}
        self._lock = asyncio.Lock()

    async def record(self, observation: Observation) -> ObservationDecision:
        decision, _ = await self._record(observation)
        return decision

    async def accept(
        self, observation: Observation, ticket_key: str, ingress_message: dict[str, Any]
    ) -> tuple[ObservationDecision, ObservationProcessingRecord | None]:
        return await self._record(observation, ticket_key, ingress_message)

    async def _record(
        self,
        observation: Observation,
        ticket_key: str | None = None,
        ingress_message: dict[str, Any] | None = None,
    ) -> tuple[ObservationDecision, ObservationProcessingRecord | None]:
        async with self._lock:
            protected = sorted(PROTECTED_WORKFLOW_FACTS & observation.facts.keys())
            if protected:
                decision = _decision(
                    observation,
                    ObservationDisposition.CONFLICT,
                    DriftClass.POLICY_BLOCKING,
                    f"external observation attempted to set workflow-owned facts: {protected}",
                )
                self._append(observation, decision)
                return decision, self._processing.get(observation.delivery_identity)
            delivery = observation.delivery_identity
            duplicate = self._deliveries.get(delivery)
            if duplicate is not None:
                if _revision_metadata_conflicts(duplicate.observation, observation):
                    decision = _decision(
                        observation,
                        ObservationDisposition.CONFLICT,
                        DriftClass.OPERATOR_REQUIRED,
                        "provider revision has inconsistent ordering metadata",
                    )
                    self._append(observation, decision)
                    return decision, self._processing.get(delivery)
                same_facts = duplicate.observation.facts == observation.facts
                decision = _decision(
                    observation,
                    ObservationDisposition.DUPLICATE
                    if same_facts
                    else ObservationDisposition.CONFLICT,
                    DriftClass.EXPECTED if same_facts else DriftClass.OPERATOR_REQUIRED,
                    "provider revision was already observed through an ingress source"
                    if same_facts
                    else "same provider revision contains different facts",
                )
                self._append(observation, decision)
                processing = self._processing.get(delivery)
                if (
                    processing is None
                    and ticket_key
                    and duplicate.disposition is ObservationDisposition.ACCEPTED
                ):
                    processing = _legacy_ambiguous_processing(
                        duplicate.observation, ticket_key, ingress_message or {}
                    )
                    self._processing[delivery] = processing
                return decision, processing

            key = resource_identity(observation)
            current = self._resources.get(key)
            disposition, drift, reason = classify_observation(
                current.latest if current else None, observation
            )
            decision = _decision(
                observation,
                disposition,
                drift,
                reason,
                supersedes=current.latest_delivery_identity
                if current and disposition is ObservationDisposition.ACCEPTED
                else None,
            )
            self._deliveries[delivery] = decision
            self._append(observation, decision)
            if disposition is ObservationDisposition.ACCEPTED:
                self._resources[key] = ReconciledResource(
                    latest=observation,
                    latest_delivery_identity=delivery,
                    updated_at=decision.decided_at,
                )
                if ticket_key:
                    processing = ObservationProcessingRecord(
                        delivery_identity=delivery,
                        observation=observation,
                        ticket_key=ticket_key,
                        ingress_message=ingress_message or {},
                        created_at=decision.decided_at,
                        updated_at=decision.decided_at,
                    )
                    self._processing[delivery] = processing
                else:
                    processing = None
            else:
                processing = None
            return decision, processing

    async def get_processing(self, delivery_identity: str) -> ObservationProcessingRecord | None:
        return self._processing.get(delivery_identity)

    async def save_processing(self, record: ObservationProcessingRecord) -> None:
        async with self._lock:
            self._processing[record.delivery_identity] = record

    async def pending_processing(
        self, *, ticket_key: str | None = None, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]:
        pending = sorted(self._processing.values(), key=lambda item: item.created_at)
        return tuple(
            item
            for item in pending
            if item.phase in _UNFINISHED_PROCESSING_PHASES
            and (ticket_key is None or item.ticket_key == ticket_key)
        )[:limit]

    async def processing_for_ticket(
        self, ticket_key: str, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]:
        records = sorted(
            (item for item in self._processing.values() if item.ticket_key == ticket_key),
            key=lambda item: item.created_at,
        )
        return tuple(records[-limit:])

    def _append(self, observation: Observation, decision: ObservationDecision) -> None:
        self._history.setdefault(resource_identity(observation), []).append(decision)
        run_id = observation_run_id(observation)
        if run_id:
            self._run_history.setdefault(run_id, []).append(decision)

    async def latest(self, observation: Observation) -> ReconciledResource | None:
        return self._resources.get(resource_identity(observation))

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]:
        return tuple(self._history.get(resource_identity(observation), ()))

    async def history_for_run(self, run_id: str) -> Sequence[ObservationDecision]:
        return tuple(self._run_history.get(run_id, ()))


class RedisObservationLedger:
    """Production ledger using optimistic transactions for monotonic acceptance."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def record(self, observation: Observation) -> ObservationDecision:
        decision, _ = await self._record(observation)
        return decision

    async def accept(
        self, observation: Observation, ticket_key: str, ingress_message: dict[str, Any]
    ) -> tuple[ObservationDecision, ObservationProcessingRecord | None]:
        return await self._record(observation, ticket_key, ingress_message)

    async def _record(
        self,
        observation: Observation,
        ticket_key: str | None = None,
        ingress_message: dict[str, Any] | None = None,
    ) -> tuple[ObservationDecision, ObservationProcessingRecord | None]:
        protected = sorted(PROTECTED_WORKFLOW_FACTS & observation.facts.keys())
        if protected:
            decision = _decision(
                observation,
                ObservationDisposition.CONFLICT,
                DriftClass.POLICY_BLOCKING,
                f"external observation attempted to set workflow-owned facts: {protected}",
            )
            await (await self._client()).rpush(
                self._history_key(observation), decision.model_dump_json()
            )
            await self._index_run(observation, decision)
            return decision, None

        redis = await self._client()
        resource_key = self._resource_key(observation)
        delivery_key = f"{_DELIVERY_PREFIX}{observation.delivery_identity}"
        processing_key = f"{_PROCESSING_PREFIX}{observation.delivery_identity}"
        while True:
            async with owned_transaction(redis) as pipeline:
                try:
                    await pipeline.watch(resource_key, delivery_key, processing_key)
                    delivery_raw = await pipeline.get(delivery_key)
                    current_raw = await pipeline.get(resource_key)
                    processing_raw = await pipeline.get(processing_key)
                    prior = (
                        ObservationDecision.model_validate_json(delivery_raw)
                        if delivery_raw
                        else None
                    )
                    current = (
                        ReconciledResource.model_validate_json(current_raw) if current_raw else None
                    )
                    processing = (
                        ObservationProcessingRecord.model_validate_json(processing_raw)
                        if processing_raw
                        else None
                    )
                    if prior is not None:
                        if _revision_metadata_conflicts(prior.observation, observation):
                            decision = _decision(
                                observation,
                                ObservationDisposition.CONFLICT,
                                DriftClass.OPERATOR_REQUIRED,
                                "provider revision has inconsistent ordering metadata",
                            )
                        else:
                            same_facts = prior.observation.facts == observation.facts
                            decision = _decision(
                                observation,
                                ObservationDisposition.DUPLICATE
                                if same_facts
                                else ObservationDisposition.CONFLICT,
                                DriftClass.EXPECTED if same_facts else DriftClass.OPERATOR_REQUIRED,
                                "provider revision was already observed through an ingress source"
                                if same_facts
                                else "same provider revision contains different facts",
                            )
                    else:
                        disposition, drift, reason = classify_observation(
                            current.latest if current else None, observation
                        )
                        decision = _decision(
                            observation,
                            disposition,
                            drift,
                            reason,
                            current.latest_delivery_identity
                            if current and disposition is ObservationDisposition.ACCEPTED
                            else None,
                        )
                    pipeline.multi()
                    pipeline.rpush(self._history_key(observation), decision.model_dump_json())
                    run_id = observation_run_id(observation)
                    if run_id:
                        pipeline.rpush(f"{_RUN_HISTORY_PREFIX}{run_id}", decision.model_dump_json())
                    if prior is None:
                        pipeline.set(delivery_key, decision.model_dump_json())
                    if decision.disposition is ObservationDisposition.ACCEPTED:
                        reconciled = ReconciledResource(
                            latest=observation,
                            latest_delivery_identity=observation.delivery_identity,
                            updated_at=decision.decided_at,
                        )
                        pipeline.set(resource_key, reconciled.model_dump_json())
                    if (
                        processing is None
                        and ticket_key
                        and decision.disposition is ObservationDisposition.ACCEPTED
                    ):
                        processing = ObservationProcessingRecord(
                            delivery_identity=observation.delivery_identity,
                            observation=observation,
                            ticket_key=ticket_key,
                            ingress_message=ingress_message or {},
                            created_at=decision.decided_at,
                            updated_at=decision.decided_at,
                        )
                        pipeline.set(processing_key, processing.model_dump_json())
                        pipeline.zadd(
                            _PROCESSING_PENDING,
                            {observation.delivery_identity: decision.decided_at.timestamp()},
                        )
                        pipeline.zadd(
                            f"{_PROCESSING_PENDING}:ticket:{ticket_key}",
                            {observation.delivery_identity: decision.decided_at.timestamp()},
                        )
                        pipeline.zadd(
                            f"{_PROCESSING_HISTORY}:ticket:{ticket_key}",
                            {observation.delivery_identity: decision.decided_at.timestamp()},
                        )
                    elif (
                        processing is None
                        and ticket_key
                        and prior is not None
                        and prior.disposition is ObservationDisposition.ACCEPTED
                    ):
                        processing = _legacy_ambiguous_processing(
                            prior.observation, ticket_key, ingress_message or {}
                        )
                        pipeline.set(processing_key, processing.model_dump_json())
                        pipeline.zadd(
                            _PROCESSING_PENDING,
                            {processing.delivery_identity: processing.created_at.timestamp()},
                        )
                        pipeline.zadd(
                            f"{_PROCESSING_PENDING}:ticket:{ticket_key}",
                            {processing.delivery_identity: processing.created_at.timestamp()},
                        )
                        pipeline.zadd(
                            f"{_PROCESSING_HISTORY}:ticket:{ticket_key}",
                            {processing.delivery_identity: processing.created_at.timestamp()},
                        )
                    await pipeline.execute()
                    if ticket_key and processing is not None:
                        await refresh_processing_metrics(redis)
                    return decision, processing
                except WatchError:
                    continue

    async def latest(self, observation: Observation) -> ReconciledResource | None:
        value = await (await self._client()).get(self._resource_key(observation))
        return ReconciledResource.model_validate_json(value) if value else None

    async def get_processing(self, delivery_identity: str) -> ObservationProcessingRecord | None:
        value = await (await self._client()).get(f"{_PROCESSING_PREFIX}{delivery_identity}")
        return ObservationProcessingRecord.model_validate_json(value) if value else None

    async def save_processing(self, record: ObservationProcessingRecord) -> None:
        redis = await self._client()
        key = f"{_PROCESSING_PREFIX}{record.delivery_identity}"
        while True:
            async with owned_transaction(redis) as pipeline:
                try:
                    await pipeline.watch(key)
                    pipeline.multi()
                    pipeline.set(key, record.model_dump_json())
                    pipeline.zadd(
                        f"{_PROCESSING_HISTORY}:ticket:{record.ticket_key}",
                        {record.delivery_identity: record.created_at.timestamp()},
                    )
                    if record.phase in _UNFINISHED_PROCESSING_PHASES:
                        pipeline.zadd(
                            _PROCESSING_PENDING,
                            {record.delivery_identity: record.created_at.timestamp()},
                        )
                        pipeline.zadd(
                            f"{_PROCESSING_PENDING}:ticket:{record.ticket_key}",
                            {record.delivery_identity: record.created_at.timestamp()},
                        )
                    else:
                        pipeline.zrem(_PROCESSING_PENDING, record.delivery_identity)
                        pipeline.zrem(
                            f"{_PROCESSING_PENDING}:ticket:{record.ticket_key}",
                            record.delivery_identity,
                        )
                    await pipeline.execute()
                    await refresh_processing_metrics(redis)
                    return
                except WatchError:
                    continue

    async def pending_processing(
        self, *, ticket_key: str | None = None, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]:
        redis = await self._client()
        index_key = (
            f"{_PROCESSING_PENDING}:ticket:{ticket_key}"
            if ticket_key is not None
            else _PROCESSING_PENDING
        )
        identities = await redis.zrange(index_key, 0, max(limit - 1, 0))
        pending: list[ObservationProcessingRecord] = []
        for identity in identities:
            if isinstance(identity, bytes):
                identity = identity.decode()
            record = await self.get_processing(str(identity))
            if record is None or record.phase not in _UNFINISHED_PROCESSING_PHASES:
                continue
            if ticket_key is None or record.ticket_key == ticket_key:
                pending.append(record)
            if len(pending) >= limit:
                break
        return tuple(pending)

    async def processing_for_ticket(
        self, ticket_key: str, limit: int = 100
    ) -> Sequence[ObservationProcessingRecord]:
        redis = await self._client()
        identities = await redis.zrange(
            f"{_PROCESSING_HISTORY}:ticket:{ticket_key}", max(-limit, -10000), -1
        )
        records: list[ObservationProcessingRecord] = []
        for identity in identities:
            if isinstance(identity, bytes):
                identity = identity.decode()
            record = await self.get_processing(str(identity))
            if record is not None:
                records.append(record)
        return tuple(records)

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]:
        values = await (await self._client()).lrange(self._history_key(observation), 0, -1)
        return tuple(ObservationDecision.model_validate_json(value) for value in values)

    async def history_for_run(self, run_id: str) -> Sequence[ObservationDecision]:
        values = await (await self._client()).lrange(f"{_RUN_HISTORY_PREFIX}{run_id}", 0, -1)
        return tuple(ObservationDecision.model_validate_json(value) for value in values)

    async def _index_run(self, observation: Observation, decision: ObservationDecision) -> None:
        run_id = observation_run_id(observation)
        if run_id:
            await (await self._client()).rpush(
                f"{_RUN_HISTORY_PREFIX}{run_id}", decision.model_dump_json()
            )

    @staticmethod
    def _resource_key(observation: Observation) -> str:
        return f"{_RESOURCE_PREFIX}{resource_identity(observation)}"

    @staticmethod
    def _history_key(observation: Observation) -> str:
        return f"{_HISTORY_PREFIX}{resource_identity(observation)}"


def classify_observation(
    current: Observation | None, incoming: Observation
) -> tuple[ObservationDisposition, DriftClass, str]:
    if current is None:
        return ObservationDisposition.ACCEPTED, DriftClass.EXPECTED, "first observed revision"
    if incoming.revision_order is not None and current.revision_order is not None:
        # The numeric order and provider token describe the same revision.  A
        # mismatch is not safely orderable: choosing either value could apply
        # facts to the wrong version or move a projection backwards.
        if (
            incoming.revision_order == current.revision_order
            and incoming.resource_revision is not None
            and current.resource_revision is not None
            and incoming.resource_revision != current.resource_revision
        ):
            return (
                ObservationDisposition.CONFLICT,
                DriftClass.OPERATOR_REQUIRED,
                "same revision order contains different provider revisions",
            )
        if (
            incoming.resource_revision is not None
            and current.resource_revision is not None
            and incoming.resource_revision == current.resource_revision
            and incoming.revision_order != current.revision_order
        ):
            return (
                ObservationDisposition.CONFLICT,
                DriftClass.OPERATOR_REQUIRED,
                "provider revision has inconsistent ordering metadata",
            )
        if incoming.revision_order < current.revision_order:
            return ObservationDisposition.STALE, DriftClass.EXPECTED, "older provider revision"
        if incoming.revision_order == current.revision_order:
            if incoming.facts == current.facts:
                return (
                    ObservationDisposition.DUPLICATE,
                    DriftClass.EXPECTED,
                    "same revision and facts",
                )
            return (
                ObservationDisposition.CONFLICT,
                DriftClass.OPERATOR_REQUIRED,
                "same provider revision contains different facts",
            )
        return (
            ObservationDisposition.ACCEPTED,
            DriftClass.AUTO_RECONCILABLE,
            "newer provider revision updates the external projection",
        )
    if incoming.resource_revision is None and current.resource_revision is None:
        return (
            ObservationDisposition.CONFLICT,
            DriftClass.OPERATOR_REQUIRED,
            "unversioned observations cannot be ordered safely",
        )
    if incoming.resource_revision == current.resource_revision:
        if incoming.facts == current.facts:
            return ObservationDisposition.DUPLICATE, DriftClass.EXPECTED, "same revision and facts"
        return (
            ObservationDisposition.CONFLICT,
            DriftClass.OPERATOR_REQUIRED,
            "opaque provider revision contains different facts",
        )
    return (
        ObservationDisposition.CONFLICT,
        DriftClass.OPERATOR_REQUIRED,
        "opaque revisions cannot be ordered safely",
    )


def _revision_metadata_conflicts(left: Observation, right: Observation) -> bool:
    """Return whether revision token and order contradict one another.

    This check is intentionally separate from delivery identity.  Ordering is
    optional metadata, so a webhook and poller may legitimately provide only
    one representation of the same revision; however, two representations
    that assert the same token at different orders (or different tokens at
    one order) cannot both be true.
    """
    if (
        left.resource_revision is None
        or right.resource_revision is None
        or left.revision_order is None
        or right.revision_order is None
    ):
        return False
    return (
        left.resource_revision == right.resource_revision
        and left.revision_order != right.revision_order
    ) or (
        left.resource_revision != right.resource_revision
        and left.revision_order == right.revision_order
    )


def _decision(
    observation: Observation,
    disposition: ObservationDisposition,
    drift: DriftClass,
    reason: str,
    supersedes: str | None = None,
) -> ObservationDecision:
    return ObservationDecision(
        observation=observation,
        delivery_identity=observation.delivery_identity,
        disposition=disposition,
        drift=drift,
        reason=reason,
        decided_at=datetime.now(UTC),
        supersedes_delivery_identity=supersedes,
    )


def _legacy_ambiguous_processing(
    observation: Observation, ticket_key: str, ingress_message: dict[str, Any]
) -> ObservationProcessingRecord:
    """Fail closed when an old accepted ledger entry has no handoff record.

    A pre-migration delivery may already have changed the checkpoint, so
    replaying it could apply an approval/retry twice. Keep it visible for
    operator reconciliation instead of guessing whether it ran.
    """
    now = datetime.now(UTC)
    return ObservationProcessingRecord(
        delivery_identity=observation.delivery_identity,
        observation=observation,
        ticket_key=ticket_key,
        ingress_message=ingress_message,
        phase=ProcessingPhase.BLOCKED,
        last_error="legacy accepted observation has no processing record; application status is ambiguous",
        created_at=now,
        updated_at=now,
    )
