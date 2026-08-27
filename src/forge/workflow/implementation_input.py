"""Compatibility facade for contract-backed implementation-input resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from forge.workflow.base import ArtifactRef, WorkUnit
from forge.workflow.projections.implementation_input import project_implementation_input
from forge.workflow.reducers.implementation_input import reduce_implementation_input
from forge.workflow.stations.implementation_input import (
    NoPendingImplementationWork as StationNoPendingImplementationWork,
)
from forge.workflow.stations.implementation_input import run_implementation_input_station


class NoPendingImplementationWork(Exception):
    """The repository has known work units, but all have already completed."""


class IssueReader(Protocol):
    """Small external-fact surface used only by the request projector."""

    async def get_issue(self, issue_key: str) -> Any: ...


@dataclass(frozen=True)
class ResolvedImplementationInput:
    """Backward-compatible view of the typed station outcome."""

    work_unit: WorkUnit
    context_artifacts: tuple[ArtifactRef, ...]
    instructions: str
    summary: str | None = None
    _station_request: Any | None = None
    _station_outcome: Any | None = None

    def state_update(self, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._station_request is not None and self._station_outcome is not None:
            return reduce_implementation_input(
                state or {}, self._station_request, self._station_outcome
            )

        existing_artifacts = list((state or {}).get("artifacts") or [])
        artifacts_by_id = {artifact.get("id"): artifact for artifact in existing_artifacts}
        for artifact in self.context_artifacts:
            artifacts_by_id[artifact.get("id")] = artifact
        existing_units = list((state or {}).get("work_units") or [])
        units_by_id = {unit.get("id"): unit for unit in existing_units}
        previous = units_by_id.get(self.work_unit["id"])
        units_by_id[self.work_unit["id"]] = (
            previous if previous and previous.get("status") == "completed" else self.work_unit
        )
        return {
            "artifacts": list(artifacts_by_id.values()),
            "work_units": list(units_by_id.values()),
            "current_work_unit_id": self.work_unit["id"],
            "work_resolution": {
                "strategy": "task_first",
                "selected_work_unit_id": self.work_unit["id"],
                "selected_artifact_id": self.work_unit["source_artifact_ids"][0],
            },
        }


async def resolve_implementation_input(
    state: Mapping[str, Any], jira: IssueReader
) -> ResolvedImplementationInput:
    """Project a checkpoint, invoke the station, and expose its compatible result."""
    try:
        request = await project_implementation_input(state, jira)
        outcome = run_implementation_input_station(request)
    except StationNoPendingImplementationWork as exc:
        raise NoPendingImplementationWork(str(exc)) from exc
    assert outcome.output is not None
    return ResolvedImplementationInput(
        work_unit=cast(WorkUnit, outcome.output.work_unit),
        context_artifacts=tuple(
            cast(ArtifactRef, item) for item in outcome.output.context_artifacts
        ),
        instructions=outcome.output.instructions,
        summary=outcome.output.summary,
        _station_request=request,
        _station_outcome=outcome,
    )
