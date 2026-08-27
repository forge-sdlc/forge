"""Provider-facing conformance fixtures for the Observation v1 contract."""

import json
from pathlib import Path

from forge.domain import Observation, ObservationSource

FIXTURES = Path(__file__).parent / "fixtures" / "observations"


def test_shared_fixture_accepts_both_ingress_sources() -> None:
    payload = json.loads((FIXTURES / "github_pull_request_revision.json").read_text())
    webhook = Observation.model_validate_json(json.dumps(payload["webhook"]))
    poller = Observation.model_validate_json(json.dumps(payload["poller"]))

    assert webhook.source is ObservationSource.WEBHOOK
    assert poller.source is ObservationSource.POLLER
    assert webhook.resource == poller.resource
    assert webhook.resource_revision == poller.resource_revision
    assert webhook.delivery_identity == poller.delivery_identity


def test_shared_fixture_is_strict_and_json_round_trips() -> None:
    payload = json.loads((FIXTURES / "github_pull_request_revision.json").read_text())
    observation = Observation.model_validate_json(json.dumps(payload["webhook"]))

    assert Observation.model_validate_json(observation.model_dump_json()) == observation
