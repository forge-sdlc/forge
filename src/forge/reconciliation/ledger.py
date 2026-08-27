"""Observation ledger with source-independent deduplication and monotonic revisions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.exceptions import WatchError

from forge.domain import Observation, stable_identity
from forge.orchestrator.checkpointer import get_redis_client
from forge.reconciliation.models import (
    DriftClass,
    ObservationDecision,
    ObservationDisposition,
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


class ObservationLedger(Protocol):
    async def record(self, observation: Observation) -> ObservationDecision: ...

    async def latest(self, observation: Observation) -> ReconciledResource | None: ...

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]: ...


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
        self._lock = asyncio.Lock()

    async def record(self, observation: Observation) -> ObservationDecision:
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
                return decision
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
                    return decision
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
                return decision

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
            return decision

    def _append(self, observation: Observation, decision: ObservationDecision) -> None:
        self._history.setdefault(resource_identity(observation), []).append(decision)

    async def latest(self, observation: Observation) -> ReconciledResource | None:
        return self._resources.get(resource_identity(observation))

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]:
        return tuple(self._history.get(resource_identity(observation), ()))


class RedisObservationLedger:
    """Production ledger using optimistic transactions for monotonic acceptance."""

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = await get_redis_client()
        return self._redis

    async def record(self, observation: Observation) -> ObservationDecision:
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
            return decision

        redis = await self._client()
        resource_key = self._resource_key(observation)
        delivery_key = f"{_DELIVERY_PREFIX}{observation.delivery_identity}"
        while True:
            async with redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(resource_key, delivery_key)
                    delivery_raw = await pipeline.get(delivery_key)
                    current_raw = await pipeline.get(resource_key)
                    prior = (
                        ObservationDecision.model_validate_json(delivery_raw)
                        if delivery_raw
                        else None
                    )
                    current = (
                        ReconciledResource.model_validate_json(current_raw) if current_raw else None
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
                    if prior is None:
                        pipeline.set(delivery_key, decision.model_dump_json())
                    if decision.disposition is ObservationDisposition.ACCEPTED:
                        reconciled = ReconciledResource(
                            latest=observation,
                            latest_delivery_identity=observation.delivery_identity,
                            updated_at=decision.decided_at,
                        )
                        pipeline.set(resource_key, reconciled.model_dump_json())
                    await pipeline.execute()
                    return decision
                except WatchError:
                    continue

    async def latest(self, observation: Observation) -> ReconciledResource | None:
        value = await (await self._client()).get(self._resource_key(observation))
        return ReconciledResource.model_validate_json(value) if value else None

    async def history(self, observation: Observation) -> Sequence[ObservationDecision]:
        values = await (await self._client()).lrange(self._history_key(observation), 0, -1)
        return tuple(ObservationDecision.model_validate_json(value) for value in values)

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
