"""Tests for complete bug workflow flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.bug.graph import (
    _route_after_analyze_bug,
    _route_after_answer_bug,
    _route_after_implementation,
    _route_after_local_review,
    _route_after_pr_creation,
    _route_after_reflect_rca,
    _route_after_teardown,
    _route_after_triage_check,
    _route_after_workspace_setup,
    _route_ci_evaluation,
    _route_human_review_bug,
    route_entry,
)
from forge.workflow.nodes.plan_bug_fix import route_plan_approval
from forge.workflow.nodes.rca_option_gate import route_rca_option
from forge.workflow.bug.state import create_initial_bug_state
from tests.fixtures.workflow_states import (
    STATE_BUG_PLAN_PENDING,
    STATE_RCA_OPTION_PENDING,
    STATE_TRIAGE_PENDING,
    make_workflow_state,
)


class TestBugWorkflowEntry:
    """Bug workflow entry routing."""

    def test_new_bug_routes_to_triage_check(self):
        """A fresh bug ticket starts at triage_check (not analyze_bug)."""
        state = create_initial_bug_state(
            thread_id="test-thread",
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
        )

        assert route_entry(state) == "triage_check"

    def test_bug_skips_prd_spec_epic_phases(self):
        """Bug workflow never routes to PRD, spec, or epic nodes."""
        state = create_initial_bug_state(
            thread_id="test-thread",
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
        )

        next_node = route_entry(state)

        assert next_node == "triage_check"
        assert next_node != "generate_prd"
        assert next_node != "generate_spec"
        assert next_node != "decompose_epics"

    def test_resume_at_rca_gate_routes_to_rca_option_gate(self):
        """Resuming at rca_approval_gate (old checkpoint) maps to rca_option_gate."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="rca_approval_gate",
            ticket_type=TicketType.BUG,
        )

        assert route_entry(state) == "rca_option_gate"

    def test_resume_at_implement_routes_there(self):
        """Resuming at implement_bug_fix returns to that node."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="implement_bug_fix",
            ticket_type=TicketType.BUG,
        )

        assert route_entry(state) == "implement_bug_fix"

    def test_terminal_state_routes_to_end(self):
        """A completed bug workflow returns END on resume attempt."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="complete",
            ticket_type=TicketType.BUG,
        )

        assert route_entry(state) == END


class TestBugImplementationRouting:
    """_route_after_implementation routes based on success/failure."""

    def test_successful_fix_routes_to_local_review(self):
        """Successful implementation routes to local_review (pre-PR code review)."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
            current_node="implement_bug_fix",
            bug_fix_implemented=True,
            last_error=None,
        )

        assert _route_after_implementation(state) == "local_review"

    def test_failed_fix_below_retry_cap_retries(self):
        """Implementation failure below retry cap retries implementation."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
            current_node="implement_bug_fix",
            bug_fix_implemented=False,
            last_error="Container timed out",
            retry_count=0,
        )

        assert _route_after_implementation(state) == "implement_bug_fix"

    def test_error_at_retry_cap_escalates(self):
        """Implementation failure at retry cap escalates to blocked."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            ticket_type=TicketType.BUG,
            current_node="implement_bug_fix",
            last_error="Container timed out",
            retry_count=3,
        )

        assert _route_after_implementation(state) == "escalate_blocked"


class TestBugWorkflowResumeRouting:
    """route_entry correctly resumes a bug workflow at any node."""

    @pytest.mark.parametrize("node,expected", [
        ("analyze_bug", "analyze_bug"),
        ("regenerate_rca", "analyze_bug"),
        ("rca_approval_gate", "rca_option_gate"),  # backward compat: old gate maps to new
        ("setup_workspace", "setup_workspace"),
        ("implement_bug_fix", "implement_bug_fix"),
        ("create_pr", "create_pr"),
        ("teardown_workspace", "teardown_workspace"),
        ("ci_evaluator", "ci_evaluator"),
        ("attempt_ci_fix", "ci_evaluator"),
        ("wait_for_ci_gate", "ci_evaluator"),  # attempt_ci_fix sets this; must not restart
        ("local_review", "local_review"),
        ("ai_review", "human_review_gate"),
        ("human_review_gate", "human_review_gate"),
        ("escalate_blocked", "escalate_blocked"),
    ])
    def test_resume_routing(self, node, expected):
        """route_entry maps each node to the correct resume target."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node=node,
            ticket_type=TicketType.BUG,
        )

        result = route_entry(state)

        assert result == expected, (
            f"route_entry with current_node='{node}' returned '{result}', "
            f"expected '{expected}'"
        )


class TestBackwardCompatFlow:
    """In-flight tickets with old current_node values route correctly."""

    def test_old_rca_approval_gate_routes_to_rca_option_gate(self):
        """State with current_node='rca_approval_gate' routes to rca_option_gate."""
        state = make_workflow_state(
            ticket_key="TEST-456",
            current_node="rca_approval_gate",
            ticket_type=TicketType.BUG,
        )
        assert route_entry(state) == "rca_option_gate"

    def test_minimal_old_state_without_new_fields_does_not_crash(self):
        """State dict missing all new fields can be passed to route_entry without KeyError."""
        minimal_old_state = {
            "ticket_key": "BUG-OLD",
            "ticket_type": "bug",
            "current_node": "implement_bug_fix",
            "is_paused": False,
        }
        result = route_entry(minimal_old_state)
        assert result == "implement_bug_fix"

    def test_all_new_current_node_values_are_handled(self):
        """Every new current_node value from the redesign has a route_entry mapping."""
        new_nodes = [
            "triage_check", "triage_gate", "reflect_rca",
            "rca_option_gate", "plan_bug_fix", "plan_approval_gate",
            "regenerate_plan", "decompose_plan", "post_merge_summary",
        ]
        for node in new_nodes:
            state = make_workflow_state(
                ticket_key="TEST-456",
                current_node=node,
                ticket_type=TicketType.BUG,
            )
            result = route_entry(state)
            assert result is not None, f"route_entry returned None for {node}"
            assert result != "", f"route_entry returned empty string for {node}"


class TestNewStateFixturesInFlow:
    """Flow-level assertions using the new bug workflow state fixtures."""

    def test_triage_pending_fixture_is_paused_at_triage_gate(self):
        """STATE_TRIAGE_PENDING is correctly paused at triage_gate."""
        assert STATE_TRIAGE_PENDING["is_paused"] is True
        assert STATE_TRIAGE_PENDING["current_node"] == "triage_gate"
        assert route_entry(STATE_TRIAGE_PENDING) == "triage_gate"

    def test_rca_option_pending_routes_to_gate(self):
        """STATE_RCA_OPTION_PENDING routes back to rca_option_gate on resume."""
        assert STATE_RCA_OPTION_PENDING["current_node"] == "rca_option_gate"
        assert route_entry(STATE_RCA_OPTION_PENDING) == "rca_option_gate"

    def test_bug_plan_pending_routes_to_plan_approval_gate(self):
        """STATE_BUG_PLAN_PENDING routes back to plan_approval_gate on resume."""
        assert STATE_BUG_PLAN_PENDING["current_node"] == "plan_approval_gate"
        assert route_entry(STATE_BUG_PLAN_PENDING) == "plan_approval_gate"


class TestNewResumeRoutingCases:
    """New pipeline nodes resume correctly at the right point."""

    @pytest.mark.parametrize("node,expected", [
        ("triage_check", "triage_check"),
        ("triage_gate", "triage_gate"),
        ("reflect_rca", "reflect_rca"),
        ("rca_option_gate", "rca_option_gate"),
        ("plan_bug_fix", "plan_bug_fix"),
        ("plan_approval_gate", "plan_approval_gate"),
        ("regenerate_plan", "regenerate_plan"),
        ("decompose_plan", "decompose_plan"),
        ("post_merge_summary", "post_merge_summary"),
        ("rca_approval_gate", "rca_option_gate"),  # backward compat
    ])
    def test_resume_routing_new_pipeline_nodes(self, node, expected):
        """route_entry maps each new current_node to the correct resume target."""
        state = make_workflow_state(
            ticket_key="BUG-99",
            current_node=node,
            ticket_type=TicketType.BUG,
        )
        assert route_entry(state) == expected

    def test_new_bug_starts_at_triage_not_analyze(self):
        """New bugs start at triage_check, not analyze_bug."""
        state = create_initial_bug_state(ticket_key="BUG-99")
        assert route_entry(state) == "triage_check"


class TestTriageLoopFlow:
    """Triage gate re-evaluates on reporter updates."""

    @pytest.mark.asyncio
    async def test_missing_fields_pauses_at_triage_gate(self):
        """triage_check with missing fields routes to triage_gate (paused)."""
        from forge.workflow.nodes.triage import triage_check

        state = make_workflow_state(
            ticket_key="BUG-T1",
            current_node="triage_check",
            ticket_type=TicketType.BUG,
            is_paused=False,
            retry_count=0,
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.get_issue = AsyncMock(return_value=MagicMock(
            summary="Login fails",
            description="Short desc",
            project_key="BUG",
        ))
        mock_jira.get_comments = AsyncMock(return_value=[])
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.run_task = AsyncMock(return_value='["steps_to_reproduce", "error_output"]')
        mock_agent.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.triage.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.triage.ForgeAgent", return_value=mock_agent),
        ):
            result = await triage_check(state)

        assert result["current_node"] == "triage_gate"
        assert result["triage_passed"] is False
        assert "steps_to_reproduce" in result.get("triage_missing_fields", [])

    @pytest.mark.asyncio
    async def test_sufficient_ticket_routes_to_analyze_bug(self):
        """triage_check with sufficient ticket routes to analyze_bug."""
        from forge.workflow.nodes.triage import triage_check

        state = make_workflow_state(
            ticket_key="BUG-T2",
            current_node="triage_check",
            ticket_type=TicketType.BUG,
            is_paused=False,
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.get_issue = AsyncMock(return_value=MagicMock(
            summary="Login fails with $", description="Full description with all fields",
            project_key="BUG",
        ))
        mock_jira.get_comments = AsyncMock(return_value=[])
        mock_jira.close = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.run_task = AsyncMock(return_value="sufficient")
        mock_agent.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.triage.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.triage.ForgeAgent", return_value=mock_agent),
        ):
            result = await triage_check(state)

        assert result["current_node"] == "analyze_bug"
        assert result["triage_passed"] is True


class TestReflectionCapFlow:
    """Reflection loop hits the 3-reflection cap."""

    @pytest.mark.asyncio
    async def test_three_failed_reflections_routes_to_rca_option_gate(self):
        """After 3 reflection failures, reflect_rca routes to rca_option_gate with warning."""
        from forge.workflow.nodes.rca_analysis import reflect_rca

        state = make_workflow_state(
            ticket_key="BUG-R1",
            current_node="reflect_rca",
            ticket_type=TicketType.BUG,
            is_paused=False,
            rca_content="## Root Cause\nBug is in validators.py",
            rca_options=[{"title": "Fix regex", "description": "Update pattern", "tradeoffs": "Low risk"}],
            reflection_count=2,  # Will become 3 after this run
            reflection_critique=None,
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.close = AsyncMock()

        class _CapRunner:
            async def run(self, workspace_path, **_kwargs):
                (workspace_path / ".forge").mkdir(exist_ok=True)
                result = MagicMock()
                result.success = True
                result.exit_code = 0
                result.stdout = "Still missing hypothesis log"
                result.stderr = ""
                return result

        with (
            patch("forge.workflow.nodes.rca_analysis.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.rca_analysis.ContainerRunner", return_value=_CapRunner()),
        ):
            result = await reflect_rca(state)

        assert result["current_node"] == "rca_option_gate"
        assert result["reflection_count"] == 3
        mock_jira.add_comment.assert_called_once()  # Warning comment posted


class TestQualitativeRetryCapFlow:
    """Qualitative review retry cap routes to create_pr with failed flag."""

    def test_qualitative_retry_count_two_routes_to_create_pr(self):
        """_route_after_local_review with qualitative_retry_count=2 → create_pr."""
        from forge.workflow.bug.graph import _route_after_local_review
        state = make_workflow_state(
            ticket_key="BUG-Q1",
            current_node="local_review",
            ticket_type=TicketType.BUG,
            local_review_verdict="tests_incomplete",
            qualitative_retry_count=2,
        )
        assert _route_after_local_review(state) == "update_documentation"

    def test_symptom_only_first_retry_routes_to_implement(self):
        """_route_after_local_review with symptom_only + retry=0 → implement_bug_fix."""
        from forge.workflow.bug.graph import _route_after_local_review
        state = make_workflow_state(
            ticket_key="BUG-Q2",
            current_node="local_review",
            ticket_type=TicketType.BUG,
            local_review_verdict="symptom_only",
            qualitative_retry_count=0,
        )
        assert _route_after_local_review(state) == "implement_bug_fix"


# ---------------------------------------------------------------------------
# Routing function tests — one class per routing function, covering all paths
# ---------------------------------------------------------------------------


class TestRouteAfterTriageCheck:
    """_route_after_triage_check routes based on current_node set by triage_check."""

    def test_missing_fields_routes_to_triage_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-TC1", ticket_type=TicketType.BUG, current_node="triage_gate",
        )
        assert _route_after_triage_check(state) == "triage_gate"

    def test_sufficient_ticket_routes_to_analyze_bug(self):
        state = make_workflow_state(
            ticket_key="BUG-TC2", ticket_type=TicketType.BUG, current_node="analyze_bug",
        )
        assert _route_after_triage_check(state) == "analyze_bug"

    def test_error_routes_to_escalate_blocked(self):
        state = make_workflow_state(
            ticket_key="BUG-TC3", ticket_type=TicketType.BUG, current_node="escalate_blocked",
        )
        assert _route_after_triage_check(state) == "escalate_blocked"

    def test_unknown_node_defaults_to_triage_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-TC4", ticket_type=TicketType.BUG, current_node="something_unknown",
        )
        assert _route_after_triage_check(state) == "triage_gate"


class TestRouteAfterAnalyzeBug:
    """_route_after_analyze_bug routes based on current_node set by analyze_bug."""

    def test_success_routes_to_reflect_rca(self):
        state = make_workflow_state(
            ticket_key="BUG-AB1", ticket_type=TicketType.BUG, current_node="reflect_rca",
        )
        assert _route_after_analyze_bug(state) == "reflect_rca"

    def test_too_many_failures_routes_to_escalate(self):
        state = make_workflow_state(
            ticket_key="BUG-AB2", ticket_type=TicketType.BUG, current_node="escalate_blocked",
        )
        assert _route_after_analyze_bug(state) == "escalate_blocked"

    def test_container_failure_terminates_invocation(self):
        state = make_workflow_state(
            ticket_key="BUG-AB3", ticket_type=TicketType.BUG, current_node="analyze_bug",
        )
        assert _route_after_analyze_bug(state) == END


class TestRouteAfterReflectRca:
    """_route_after_reflect_rca handles reflection loop and failure states."""

    def test_failure_state_routes_to_escalate(self):
        state = make_workflow_state(
            ticket_key="BUG-RR1", ticket_type=TicketType.BUG, current_node="escalate_blocked",
        )
        assert _route_after_reflect_rca(state) == "escalate_blocked"

    def test_container_failure_terminates(self):
        state = make_workflow_state(
            ticket_key="BUG-RR2", ticket_type=TicketType.BUG, current_node="reflect_rca",
        )
        assert _route_after_reflect_rca(state) == END

    def test_reflection_cap_routes_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-RR3", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            reflection_count=3, reflection_critique="still needs depth",
        )
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_critique_below_cap_loops_to_analyze_bug(self):
        state = make_workflow_state(
            ticket_key="BUG-RR4", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            reflection_count=1, reflection_critique="needs more depth on auth flow",
        )
        assert _route_after_reflect_rca(state) == "analyze_bug"

    def test_no_critique_routes_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-RR5", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            reflection_count=1, reflection_critique=None,
        )
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_empty_critique_routes_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-RR6", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            reflection_count=1, reflection_critique="",
        )
        assert _route_after_reflect_rca(state) == "rca_option_gate"

    def test_whitespace_only_critique_routes_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-RR7", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            reflection_count=1, reflection_critique="   ",
        )
        assert _route_after_reflect_rca(state) == "rca_option_gate"


class TestRouteRcaOption:
    """route_rca_option routes from rca_option_gate based on workflow signals."""

    def test_question_routes_to_answer_question(self):
        state = make_workflow_state(
            ticket_key="BUG-RO1", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            is_question=True,
        )
        assert route_rca_option(state) == "answer_question"

    def test_question_takes_priority_over_selection(self):
        state = make_workflow_state(
            ticket_key="BUG-RO2", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            is_question=True, selected_fix_option=1, is_paused=False,
        )
        assert route_rca_option(state) == "answer_question"

    def test_option_selected_routes_to_plan_bug_fix(self):
        state = make_workflow_state(
            ticket_key="BUG-RO3", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            selected_fix_option=1, is_paused=False,
        )
        assert route_rca_option(state) == "plan_bug_fix"

    def test_option_selected_while_paused_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-RO4", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            selected_fix_option=1, is_paused=True,
        )
        assert route_rca_option(state) == END

    def test_revision_requested_routes_to_regenerate_rca(self):
        state = make_workflow_state(
            ticket_key="BUG-RO5", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            revision_requested=True, is_paused=False,
        )
        assert route_rca_option(state) == "regenerate_rca"

    def test_paused_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-RO6", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            is_paused=True,
        )
        assert route_rca_option(state) == END

    def test_no_signals_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-RO7", ticket_type=TicketType.BUG, current_node="rca_option_gate",
            is_paused=False,
        )
        assert route_rca_option(state) == END


class TestRoutePlanApproval:
    """route_plan_approval routes from plan_approval_gate."""

    def test_question_routes_to_answer_question(self):
        state = make_workflow_state(
            ticket_key="BUG-PA1", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
            is_question=True,
        )
        assert route_plan_approval(state) == "answer_question"

    def test_paused_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-PA2", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
            is_paused=True,
        )
        assert route_plan_approval(state) == END

    def test_revision_requested_routes_to_regenerate_plan(self):
        state = make_workflow_state(
            ticket_key="BUG-PA3", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
            revision_requested=True, is_paused=False,
        )
        assert route_plan_approval(state) == "regenerate_plan"

    def test_approved_routes_to_decompose_plan(self):
        state = make_workflow_state(
            ticket_key="BUG-PA4", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
            is_paused=False, revision_requested=False,
        )
        assert route_plan_approval(state) == "decompose_plan"

    def test_question_takes_priority_over_paused(self):
        state = make_workflow_state(
            ticket_key="BUG-PA5", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
            is_question=True, is_paused=True,
        )
        assert route_plan_approval(state) == "answer_question"


class TestRouteAfterWorkspaceSetup:
    """_route_after_workspace_setup routes based on workspace_path and last_error."""

    def test_success_routes_to_implement(self):
        state = make_workflow_state(
            ticket_key="BUG-WS1", ticket_type=TicketType.BUG, current_node="setup_workspace",
            workspace_path="/tmp/forge-ws", last_error=None,
        )
        assert _route_after_workspace_setup(state) == "implement_bug_fix"

    def test_no_workspace_path_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-WS2", ticket_type=TicketType.BUG, current_node="setup_workspace",
            workspace_path=None, last_error=None,
        )
        assert _route_after_workspace_setup(state) == "escalate_blocked"

    def test_error_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-WS3", ticket_type=TicketType.BUG, current_node="setup_workspace",
            workspace_path="/tmp/forge-ws", last_error="clone failed",
        )
        assert _route_after_workspace_setup(state) == "escalate_blocked"

    def test_empty_workspace_path_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-WS4", ticket_type=TicketType.BUG, current_node="setup_workspace",
            workspace_path="", last_error=None,
        )
        assert _route_after_workspace_setup(state) == "escalate_blocked"


class TestRouteAfterImplementation:
    """_route_after_implementation routes based on last_error and retry_count."""

    def test_no_error_routes_to_local_review(self):
        state = make_workflow_state(
            ticket_key="BUG-IM1", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
            last_error=None, retry_count=0,
        )
        assert _route_after_implementation(state) == "local_review"

    def test_error_below_cap_retries(self):
        state = make_workflow_state(
            ticket_key="BUG-IM2", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
            last_error="timeout", retry_count=1,
        )
        assert _route_after_implementation(state) == "implement_bug_fix"

    def test_error_at_cap_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-IM3", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
            last_error="timeout", retry_count=3,
        )
        assert _route_after_implementation(state) == "escalate_blocked"

    def test_error_above_cap_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-IM4", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
            last_error="timeout", retry_count=5,
        )
        assert _route_after_implementation(state) == "escalate_blocked"

    def test_no_error_ignores_high_retry_count(self):
        state = make_workflow_state(
            ticket_key="BUG-IM5", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
            last_error=None, retry_count=5,
        )
        assert _route_after_implementation(state) == "local_review"


class TestRouteAfterLocalReview:
    """_route_after_local_review routes based on verdict and retry counts."""

    def test_adequate_verdict_routes_to_update_docs(self):
        state = make_workflow_state(
            ticket_key="BUG-LR1", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict="adequate", qualitative_retry_count=0,
        )
        assert _route_after_local_review(state) == "update_documentation"

    def test_tests_incomplete_routes_to_implement(self):
        state = make_workflow_state(
            ticket_key="BUG-LR2", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict="tests_incomplete", qualitative_retry_count=0,
        )
        assert _route_after_local_review(state) == "implement_bug_fix"

    def test_symptom_only_routes_to_implement(self):
        state = make_workflow_state(
            ticket_key="BUG-LR3", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict="symptom_only", qualitative_retry_count=0,
        )
        assert _route_after_local_review(state) == "implement_bug_fix"

    def test_tests_incomplete_at_cap_routes_to_update_docs(self):
        state = make_workflow_state(
            ticket_key="BUG-LR4", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict="tests_incomplete", qualitative_retry_count=2,
        )
        assert _route_after_local_review(state) == "update_documentation"

    def test_no_verdict_mechanical_at_cap_routes_to_update_docs(self):
        state = make_workflow_state(
            ticket_key="BUG-LR5", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict=None, local_review_attempts=2,
        )
        assert _route_after_local_review(state) == "update_documentation"

    def test_no_verdict_mechanical_below_cap_falls_back_to_current_node(self):
        state = make_workflow_state(
            ticket_key="BUG-LR6", ticket_type=TicketType.BUG, current_node="local_review",
            local_review_verdict=None, local_review_attempts=0,
        )
        assert _route_after_local_review(state) == "local_review"


class TestRouteAfterPrCreation:
    """_route_after_pr_creation routes based on last_error and pr_urls."""

    def test_success_routes_to_teardown(self):
        state = make_workflow_state(
            ticket_key="BUG-PR1", ticket_type=TicketType.BUG, current_node="create_pr",
            last_error=None, pr_urls=["https://github.com/org/repo/pull/1"],
        )
        assert _route_after_pr_creation(state) == "teardown_workspace"

    def test_error_with_no_pr_urls_escalates(self):
        state = make_workflow_state(
            ticket_key="BUG-PR2", ticket_type=TicketType.BUG, current_node="create_pr",
            last_error="PR creation failed", pr_urls=[],
        )
        assert _route_after_pr_creation(state) == "escalate_blocked"

    def test_error_with_existing_pr_urls_routes_to_teardown(self):
        state = make_workflow_state(
            ticket_key="BUG-PR3", ticket_type=TicketType.BUG, current_node="create_pr",
            last_error="partial failure", pr_urls=["https://github.com/org/repo/pull/1"],
        )
        assert _route_after_pr_creation(state) == "teardown_workspace"

    def test_no_error_no_pr_urls_routes_to_teardown(self):
        state = make_workflow_state(
            ticket_key="BUG-PR4", ticket_type=TicketType.BUG, current_node="create_pr",
            last_error=None, pr_urls=[],
        )
        assert _route_after_pr_creation(state) == "teardown_workspace"


class TestRouteAfterTeardown:
    """_route_after_teardown loops back for remaining repos or proceeds to CI."""

    def test_remaining_repos_loops_to_setup_workspace(self):
        state = make_workflow_state(
            ticket_key="BUG-TD1", ticket_type=TicketType.BUG, current_node="teardown_workspace",
            repos_to_process=["org/a", "org/b"], repos_completed=["org/a"],
        )
        assert _route_after_teardown(state) == "setup_workspace"

    def test_all_repos_done_routes_to_ci_evaluator(self):
        state = make_workflow_state(
            ticket_key="BUG-TD2", ticket_type=TicketType.BUG, current_node="teardown_workspace",
            repos_to_process=["org/a"], repos_completed=["org/a"],
        )
        assert _route_after_teardown(state) == "ci_evaluator"

    def test_empty_repos_routes_to_ci_evaluator(self):
        state = make_workflow_state(
            ticket_key="BUG-TD3", ticket_type=TicketType.BUG, current_node="teardown_workspace",
            repos_to_process=[], repos_completed=[],
        )
        assert _route_after_teardown(state) == "ci_evaluator"

    def test_multiple_remaining_repos_loops(self):
        state = make_workflow_state(
            ticket_key="BUG-TD4", ticket_type=TicketType.BUG, current_node="teardown_workspace",
            repos_to_process=["org/a", "org/b", "org/c"], repos_completed=[],
        )
        assert _route_after_teardown(state) == "setup_workspace"


class TestRouteCiEvaluation:
    """_route_ci_evaluation routes based on ci_status."""

    def test_passed_routes_to_human_review_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-CI1", ticket_type=TicketType.BUG, current_node="ci_evaluator",
            ci_status="passed",
        )
        assert _route_ci_evaluation(state) == "human_review_gate"

    def test_fixing_routes_to_attempt_ci_fix(self):
        state = make_workflow_state(
            ticket_key="BUG-CI2", ticket_type=TicketType.BUG, current_node="ci_evaluator",
            ci_status="fixing",
        )
        assert _route_ci_evaluation(state) == "attempt_ci_fix"

    def test_pending_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-CI3", ticket_type=TicketType.BUG, current_node="ci_evaluator",
            ci_status="pending",
        )
        assert _route_ci_evaluation(state) == END

    def test_failed_routes_to_escalate_blocked(self):
        state = make_workflow_state(
            ticket_key="BUG-CI4", ticket_type=TicketType.BUG, current_node="ci_evaluator",
            ci_status="failed",
        )
        assert _route_ci_evaluation(state) == "escalate_blocked"

    def test_empty_status_routes_to_escalate_blocked(self):
        state = make_workflow_state(
            ticket_key="BUG-CI5", ticket_type=TicketType.BUG, current_node="ci_evaluator",
            ci_status="",
        )
        assert _route_ci_evaluation(state) == "escalate_blocked"


class TestRouteHumanReviewBug:
    """_route_human_review_bug intercepts pr_merged for post_merge_summary."""

    def test_pr_merged_routes_to_post_merge_summary(self):
        state = make_workflow_state(
            ticket_key="BUG-HR1", ticket_type=TicketType.BUG, current_node="human_review_gate",
            pr_merged=True,
        )
        assert _route_human_review_bug(state) == "post_merge_summary"

    def test_revision_requested_routes_to_implement_review(self):
        state = make_workflow_state(
            ticket_key="BUG-HR2", ticket_type=TicketType.BUG, current_node="human_review_gate",
            pr_merged=False, revision_requested=True, feedback_comment="fix the tests",
        )
        assert _route_human_review_bug(state) == "implement_review"

    def test_paused_routes_to_end(self):
        state = make_workflow_state(
            ticket_key="BUG-HR3", ticket_type=TicketType.BUG, current_node="human_review_gate",
            pr_merged=False, is_paused=True,
        )
        assert _route_human_review_bug(state) == END

    def test_not_merged_not_paused_routes_to_complete_tasks(self):
        state = make_workflow_state(
            ticket_key="BUG-HR4", ticket_type=TicketType.BUG, current_node="human_review_gate",
            pr_merged=False, is_paused=False, revision_requested=False,
        )
        assert _route_human_review_bug(state) == "complete_tasks"

    def test_pr_merged_takes_priority_over_revision(self):
        state = make_workflow_state(
            ticket_key="BUG-HR5", ticket_type=TicketType.BUG, current_node="human_review_gate",
            pr_merged=True, revision_requested=True, feedback_comment="fix",
        )
        assert _route_human_review_bug(state) == "post_merge_summary"


class TestRouteAfterAnswerBug:
    """_route_after_answer_bug routes back to the correct gate."""

    def test_returns_to_triage_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-AQ1", ticket_type=TicketType.BUG, current_node="triage_gate",
        )
        assert _route_after_answer_bug(state) == "triage_gate"

    def test_returns_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-AQ2", ticket_type=TicketType.BUG, current_node="rca_option_gate",
        )
        assert _route_after_answer_bug(state) == "rca_option_gate"

    def test_returns_to_plan_approval_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-AQ3", ticket_type=TicketType.BUG, current_node="plan_approval_gate",
        )
        assert _route_after_answer_bug(state) == "plan_approval_gate"

    def test_unknown_node_defaults_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-AQ4", ticket_type=TicketType.BUG, current_node="implement_bug_fix",
        )
        assert _route_after_answer_bug(state) == "rca_option_gate"

    def test_empty_node_defaults_to_rca_option_gate(self):
        state = make_workflow_state(
            ticket_key="BUG-AQ5", ticket_type=TicketType.BUG, current_node="",
        )
        assert _route_after_answer_bug(state) == "rca_option_gate"
