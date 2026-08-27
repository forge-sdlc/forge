"""Typed local and control-plane runner for contract-backed stations."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.domain import DomainModel, StationOutcome, StationRequest
from forge.effects import EffectService
from forge.workflow.stations.approval import ApprovalInput, run_approval_station
from forge.workflow.stations.artifact_generation import (
    ArtifactGenerationInput,
    run_artifact_generation_station,
)
from forge.workflow.stations.implementation_input import (
    ImplementationInput,
    run_implementation_input_station,
)
from forge.workflow.stations.task_routing import (
    RepositoryAggregationInput,
    TaskRoutingInput,
    run_repository_aggregation_station,
    run_task_routing_station,
)
from forge.workflow.stations.triage import TriageInput, run_triage_station

StationHandler = Callable[
    [StationRequest[Any]], StationOutcome[Any] | Awaitable[StationOutcome[Any]]
]


@dataclass(frozen=True)
class StationDefinition:
    name: str
    contract_version: str
    input_type: type[DomainModel]
    handler: StationHandler


class StationRegistry:
    """Local registry shared by central and standalone station execution."""

    def __init__(self) -> None:
        self._definitions: dict[str, StationDefinition] = {}

    def register(self, definition: StationDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Station already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def resolve(self, name: str) -> StationDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"Unknown station: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


def create_builtin_station_registry() -> StationRegistry:
    registry = StationRegistry()
    registry.register(
        StationDefinition("approval-policy", "1.0", ApprovalInput, run_approval_station)
    )
    registry.register(
        StationDefinition(
            "artifact-generation",
            "1.0",
            ArtifactGenerationInput,
            run_artifact_generation_station,
        )
    )
    registry.register(
        StationDefinition(
            "implementation-input", "1.0", ImplementationInput, run_implementation_input_station
        )
    )
    registry.register(
        StationDefinition("task-routing", "1.0", TaskRoutingInput, run_task_routing_station)
    )
    registry.register(
        StationDefinition("triage-evaluation", "1.0", TriageInput, run_triage_station)
    )
    registry.register(
        StationDefinition(
            "repository-result-aggregation",
            "1.0",
            RepositoryAggregationInput,
            run_repository_aggregation_station,
        )
    )
    return registry


async def invoke_station(
    definition: StationDefinition,
    request: StationRequest[Any],
    *,
    effect_service: EffectService | None = None,
) -> StationOutcome[Any]:
    """Validate, invoke, and durably complete required effects before returning."""
    if request.contract_name != definition.name:
        raise ValueError("Station request contract name does not match registration")
    if request.contract_version != definition.contract_version:
        raise ValueError("Station request contract version is not supported")
    if not isinstance(request.input, definition.input_type):
        raise ValueError("Station request input does not match its registered contract")
    candidate = definition.handler(request)
    outcome = await candidate if inspect.isawaitable(candidate) else candidate
    if outcome.workflow != request.workflow or outcome.invocation != request.invocation:
        raise ValueError("Station outcome does not belong to its request")
    if (outcome.contract_name, outcome.contract_version) != (
        request.contract_name,
        request.contract_version,
    ):
        raise ValueError("Station outcome contract does not match its request")
    if outcome.requested_effects and effect_service is None:
        raise ValueError("Station requested effects but no durable effect service was supplied")
    for effect in outcome.requested_effects:
        if effect.workflow != request.workflow:
            raise ValueError("Station effect does not belong to its workflow")
        assert effect_service is not None
        await effect_service.execute_required(effect)
    return outcome


async def run_serialized_async(
    station_name: str,
    request_json: str,
    *,
    registry: StationRegistry | None = None,
    effect_service: EffectService | None = None,
) -> str:
    """Run a station from serialized input without the Forge control plane."""
    definition = (registry or create_builtin_station_registry()).resolve(station_name)
    request_type = StationRequest[definition.input_type]  # type: ignore[valid-type]
    request = request_type.model_validate_json(request_json)
    outcome = await invoke_station(definition, request, effect_service=effect_service)
    return outcome.model_dump_json()


def run_serialized(station_name: str, request_json: str) -> str:
    """Synchronous convenience entry point for local fixtures and CLI callers."""
    return asyncio.run(run_serialized_async(station_name, request_json))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("station")
    parser.add_argument("request", nargs="?", help="Request JSON file; defaults to stdin")
    args = parser.parse_args()
    request_json = Path(args.request).read_text() if args.request else sys.stdin.read()
    print(run_serialized(args.station, request_json))


if __name__ == "__main__":
    main()
