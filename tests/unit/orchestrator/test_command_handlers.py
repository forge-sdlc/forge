from datetime import UTC, datetime

from forge.domain import WorkflowCommand, WorkflowCommandType, WorkflowIdentity
from forge.orchestrator.command_handlers import (
    FeedbackKind,
    create_default_command_handler_registry,
)


def _command(command_type: WorkflowCommandType, **arguments) -> WorkflowCommand:
    return WorkflowCommand(
        command_id=f"command-{command_type.value}",
        command_type=command_type,
        workflow=WorkflowIdentity(
            run_id="FORGE-1", workflow_name="feature", definition_revision=1
        ),
        requested_at=datetime(2026, 8, 27, tzinfo=UTC),
        arguments=arguments,
    )


def test_skip_gate_application_is_provider_neutral() -> None:
    application = create_default_command_handler_registry().apply(
        _command(WorkflowCommandType.SKIP_GATE, check_name="lint", sender="alice"),
        {"current_node": "human_review_gate", "ci_skipped_checks": []},
    )

    assert application is not None
    assert application.state["ci_skipped_checks"] == ["lint"]
    assert application.state["current_node"] == "ci_evaluator"
    assert application.feedback is not None
    assert application.feedback.kind is FeedbackKind.SKIP_GATE


def test_rebase_preserves_return_position() -> None:
    application = create_default_command_handler_registry().apply(
        _command(WorkflowCommandType.REBASE, sender="alice"),
        {"current_node": "human_review_gate", "current_pr_number": 7},
    )

    assert application is not None
    assert application.state["current_node"] == "rebase_pr"
    assert application.state["rebase_return_node"] == "human_review_gate"


def test_select_option_validates_against_authoritative_state() -> None:
    registry = create_default_command_handler_registry()
    valid = registry.apply(
        _command(WorkflowCommandType.SELECT_OPTION, option=2),
        {"current_node": "rca_option_gate", "rca_options": ["a", "b"]},
    )
    invalid = registry.apply(
        _command(WorkflowCommandType.SELECT_OPTION, option=3),
        {"current_node": "rca_option_gate", "rca_options": ["a", "b"]},
    )

    assert valid is not None
    assert valid.state["selected_fix_approach"] == "b"
    assert invalid is None
