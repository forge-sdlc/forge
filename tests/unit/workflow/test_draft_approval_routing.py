"""Regression coverage for approving review-only Jira drafts."""

from forge.workflow.gates.plan_approval import route_plan_approval
from forge.workflow.gates.task_approval import route_task_approval


def test_approved_epic_draft_routes_to_provisioning() -> None:
    state = {
        "ticket_key": "AISOS-1",
        "current_node": "plan_approval_gate",
        "is_paused": False,
        "epic_keys": [],
        "plan_draft": {"items": [{"id": 1}]},
    }

    assert route_plan_approval(state) == "provision_epics"


def test_approved_task_draft_routes_to_provisioning() -> None:
    state = {
        "ticket_key": "AISOS-1",
        "current_node": "task_approval_gate",
        "is_paused": False,
        "task_keys": [],
        "tasks_draft": {"items": [{"id": 1}]},
    }

    assert route_task_approval(state) == "provision_tasks"
