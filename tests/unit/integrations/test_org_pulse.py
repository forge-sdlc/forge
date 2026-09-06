from datetime import UTC, datetime

from forge.integrations.org_pulse import OrgPulseExecution
from forge.read_models.execution import project_execution


def test_org_pulse_contract_is_versioned_and_contains_operational_state() -> None:
    model = project_execution(
        {
            "thread_id": "FORGE-7",
            "ticket_key": "FORGE-7",
            "workflow_name": "feature",
            "workflow_revision": 4,
            "current_node": "approval_gate",
            "is_paused": True,
            "updated_at": datetime(2026, 8, 28, tzinfo=UTC).isoformat(),
            "station_history": [
                {"station_name": "approval", "invocation_id": "a-1", "attempt": 2}
            ],
        }
    )

    pulse = OrgPulseExecution.from_execution(model)

    assert pulse.schema_version == "1.0"
    assert pulse.ticket_key == "FORGE-7"
    assert pulse.status == "waiting"
    assert pulse.waiting_code == "gate"
    assert pulse.retry_count == 1
    assert pulse.migration_eligible is None
