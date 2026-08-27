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


def run_serialized(station_name: str, request_json: str) -> str:
    """Run a station from serialized input without the Forge control plane."""
    if station_name != "implementation-input":
        raise ValueError(f"Unknown station: {station_name}")
    request = StationRequest[ImplementationInput].model_validate_json(request_json)
    return run_implementation_input_station(request).model_dump_json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("station")
    parser.add_argument("request", nargs="?", help="Request JSON file; defaults to stdin")
    args = parser.parse_args()
    request_json = Path(args.request).read_text() if args.request else sys.stdin.read()
    print(run_serialized(args.station, request_json))


if __name__ == "__main__":
    main()
