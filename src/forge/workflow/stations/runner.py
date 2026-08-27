"""Minimal local runner for contract-backed stations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from forge.domain import StationRequest
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


def run_serialized(station_name: str, request_json: str) -> str:
    """Run a station from serialized input without the Forge control plane."""
    if station_name == "implementation-input":
        request = StationRequest[ImplementationInput].model_validate_json(request_json)
        return run_implementation_input_station(request).model_dump_json()
    if station_name == "task-routing":
        routing_request = StationRequest[TaskRoutingInput].model_validate_json(request_json)
        return run_task_routing_station(routing_request).model_dump_json()
    if station_name == "repository-result-aggregation":
        aggregation_request = StationRequest[RepositoryAggregationInput].model_validate_json(
            request_json
        )
        return run_repository_aggregation_station(aggregation_request).model_dump_json()
    raise ValueError(f"Unknown station: {station_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("station")
    parser.add_argument("request", nargs="?", help="Request JSON file; defaults to stdin")
    args = parser.parse_args()
    request_json = Path(args.request).read_text() if args.request else sys.stdin.read()
    print(run_serialized(args.station, request_json))


if __name__ == "__main__":
    main()
