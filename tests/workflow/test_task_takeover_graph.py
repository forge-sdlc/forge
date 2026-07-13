"""Unit and integration tests for Task Takeover workflow graph and routing."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END, StateGraph

from forge.models.workflow import ForgeLabel, TicketType
from forge.workflow.gates.task_plan_approval import route_task_plan_approval
from forge.workflow.task_takeover.graph import (
    _route_after_answer,
    _route_after_generate_plan,
    _route_after_triage_check,
    _route_ci_evaluation,
    _route_human_review_task_takeover,
    build_task_takeover_graph,
    complete_task_takeover,
    route_entry,
)
from forge.workflow.task_takeover.state import (
    TaskTakeoverState,
)


def make_task_state(**overrides: Any) -> TaskTakeoverState:
    """Create a TaskTakeoverState dict for graph tests."""
    base = {
        "ticket_key": "TASK-123",
        "ticket_type": TicketType.TASK,
        "current_node": "start",
        "is_paused": False,
        "retry_count": 0,
        "last_error": None,
        "triage_passed": False,
        "triage_missing_fields": [],
        "plan_content": None,
    }
    return cast(TaskTakeoverState, {**base, **overrides})


class TestTaskTakeoverGraphStructure:
    """Test LangGraph StateGraph structure and compilation."""

    def test_graph_compilation_and_nodes(self) -> None:
        """Verify the graph compiles and contains the correct nodes and transitions."""
        graph = build_task_takeover_graph()
        assert isinstance(graph, StateGraph)

        compiled_graph = graph.compile()
        assert compiled_graph is not None

        # Verify expected nodes are present in the compiled graph
        expected_nodes = {
            "route_entry",
            "triage_check",
            "triage_gate",
            "generate_plan",
            "task_plan_approval_gate",
            "escalate_blocked",
            "answer_question",
            "setup_workspace",
            "execute_task_changes",
            "run_qualitative_review",
            "create_pr",
            "teardown_workspace",
            "wait_for_ci_gate",
            "ci_evaluator",
            "attempt_ci_fix",
            "human_review_gate",
            "implement_review",
            "review_response_gate",
            "complete_task_takeover",
        }
        for node in expected_nodes:
            assert node in compiled_graph.nodes


class TestPathTransitions:
    """Test path transitions and route entry logic for state progression."""

    @pytest.mark.parametrize(
        "current_node, expected_next",
        [
            ("triage_check", "triage_check"),
            ("triage_gate", "triage_gate"),
            ("generate_plan", "generate_plan"),
            ("task_plan_approval_gate", "task_plan_approval_gate"),
            ("escalate_blocked", "escalate_blocked"),
            ("setup_workspace", "setup_workspace"),
            ("execute_task_changes", "execute_task_changes"),
            ("qualitative_review", "run_qualitative_review"),
            ("create_pr", "create_pr"),
            ("teardown_workspace", "teardown_workspace"),
            ("wait_for_ci_gate", "wait_for_ci_gate"),
            ("ci_evaluator", "ci_evaluator"),
            ("attempt_ci_fix", "ci_evaluator"),
            ("human_review_gate", "human_review_gate"),
            ("implement_review", "implement_review"),
            ("review_response_gate", "review_response_gate"),
            ("complete", END),
            ("", "triage_check"),
            ("unknown_node", "triage_check"),
        ],
    )
    def test_route_entry(self, current_node: str, expected_next: str) -> None:
        """Verify that route_entry resumes at the appropriate node or restarts from triage."""
        state = make_task_state(current_node=current_node)
        assert route_entry(state) == expected_next

    @pytest.mark.parametrize(
        "current_node, expected_next",
        [
            ("triage_check", "triage_check"),
            ("generate_plan", "generate_plan"),
            ("triage_gate", "triage_gate"),
            ("escalate_blocked", "escalate_blocked"),
            ("unknown_node", "triage_gate"),
        ],
    )
    def test_route_after_triage_check(self, current_node: str, expected_next: str) -> None:
        """Verify route_after_triage_check path routing."""
        state = make_task_state(current_node=current_node)
        assert _route_after_triage_check(state) == expected_next

    @pytest.mark.parametrize(
        "current_node, expected_next",
        [
            ("task_plan_approval_gate", "task_plan_approval_gate"),
            ("", "task_plan_approval_gate"),
            ("some_other_gate", "some_other_gate"),
        ],
    )
    def test_route_after_answer(self, current_node: str, expected_next: str) -> None:
        """Verify route_after_answer returns back to the original gate."""
        state = make_task_state(current_node=current_node)
        assert _route_after_answer(state) == expected_next

    def test_route_after_generate_plan_success_routes_to_approval(self) -> None:
        state = make_task_state(current_node="task_plan_approval_gate", last_error=None)
        assert _route_after_generate_plan(state) == "task_plan_approval_gate"

    def test_route_after_generate_plan_failure_retries_same_node(self) -> None:
        state = make_task_state(
            current_node="generate_plan",
            last_error="container failed",
            retry_count=1,
        )
        assert _route_after_generate_plan(state) == "generate_plan"

    def test_route_after_generate_plan_retry_cap_routes_to_blocked(self) -> None:
        state = make_task_state(
            current_node="generate_plan",
            last_error="container failed",
            retry_count=3,
        )
        assert _route_after_generate_plan(state) == "escalate_blocked"


class TestQualitativeReviewRouting:
    """Test routing after run_qualitative_review."""

    def test_route_after_qualitative_review_adequate(self) -> None:
        """If review is adequate, proceed to PR creation."""
        from forge.workflow.task_takeover.graph import _route_after_qualitative_review

        state = make_task_state(
            review_verdict="adequate",
            qualitative_review_retry_count=0,
        )
        assert _route_after_qualitative_review(state) == "create_pr"

    def test_route_after_qualitative_review_failed_under_limit(self) -> None:
        """If review is failed or incomplete and under the limit, route back to execute_task_changes."""
        from forge.workflow.task_takeover.graph import _route_after_qualitative_review

        state = make_task_state(
            review_verdict="tests_incomplete",
            qualitative_review_retry_count=1,
        )
        # The task takeover qualitative review retry limit is 2, so retry_count of 1 is under the limit.
        assert _route_after_qualitative_review(state) == "execute_task_changes"

    def test_route_after_qualitative_review_failed_at_or_above_limit(self) -> None:
        """If review is failed or incomplete and at/above the limit, proceed to PR creation."""
        from forge.workflow.task_takeover.graph import _route_after_qualitative_review

        state = make_task_state(
            review_verdict="tests_incomplete",
            qualitative_review_retry_count=2,
        )
        # retry_count of 2 is at/above the limit of 2, so stop retrying but keep Jira silent.
        assert _route_after_qualitative_review(state) == "create_pr"

    def test_route_after_qualitative_review_error_without_verdict_escalates(self) -> None:
        """If review hit an error without producing a verdict, escalate to blocked."""
        from forge.workflow.task_takeover.graph import _route_after_qualitative_review

        state = make_task_state(
            last_error="Workspace not set up",
            qualitative_review_retry_count=0,
        )
        assert _route_after_qualitative_review(state) == "escalate_blocked"


class TestPostPrRouting:
    """Test Task Takeover post-PR CI and review routing."""

    @pytest.mark.parametrize(
        "ci_status, expected",
        [
            ("passed", "human_review_gate"),
            ("fixing", "attempt_ci_fix"),
            ("pending", END),
            ("failed", "escalate_blocked"),
            ("", "escalate_blocked"),
        ],
    )
    def test_route_ci_evaluation(self, ci_status: str, expected: str) -> None:
        state = make_task_state(ci_status=ci_status)
        assert _route_ci_evaluation(state) == expected

    def test_human_review_merge_routes_to_task_takeover_complete(self) -> None:
        state = make_task_state(pr_merged=True, current_node="human_review_gate")
        assert _route_human_review_task_takeover(state) == "complete_task_takeover"

    def test_human_review_changes_requested_routes_to_implement_review(self) -> None:
        state = make_task_state(
            current_node="human_review_gate",
            revision_requested=True,
            feedback_comment="Please address this review feedback.",
        )
        assert _route_human_review_task_takeover(state) == "implement_review"

    def test_human_review_paused_routes_to_end(self) -> None:
        state = make_task_state(current_node="human_review_gate", is_paused=True)
        assert _route_human_review_task_takeover(state) == END

    def test_human_review_approved_routes_to_task_takeover_complete(self) -> None:
        state = make_task_state(current_node="human_review_gate", is_paused=False)
        assert _route_human_review_task_takeover(state) == "complete_task_takeover"

    @pytest.mark.asyncio
    async def test_complete_task_takeover_marks_workflow_complete(self) -> None:
        state = make_task_state(current_node="human_review_gate", is_paused=True)
        result = await complete_task_takeover(state)
        assert result["current_node"] == "complete"
        assert result["is_paused"] is False
        assert result["ci_fix_attempt"] == 0


class TestInteractiveGateBehavior:
    """Test interactive gate behavior for plan approvals, questions, and revision requests."""

    @pytest.fixture
    def paused_state(self) -> TaskTakeoverState:
        return make_task_state(
            current_node="task_plan_approval_gate",
            is_paused=True,
        )

    def test_gate_remains_paused_waiting_for_updates(self, paused_state: TaskTakeoverState) -> None:
        """If still paused and no revision/question signals exist, stay paused (END)."""
        result = route_task_plan_approval(paused_state)
        assert result == END

    def test_gate_routes_to_answer_question_on_prefix(
        self, paused_state: TaskTakeoverState
    ) -> None:
        """Comment prefixed with '?' or '@forge ask' routes to answer_question."""
        # 1. Direct bool flag
        state_bool = {**paused_state, "is_question": True}
        assert route_task_plan_approval(state_bool) == "answer_question"

        # 2. '?' prefix comment
        state_q = {**paused_state, "feedback_comment": "?Can we run this in parallel?"}
        assert route_task_plan_approval(state_q) == "answer_question"

        # 3. '@forge ask' prefix comment
        state_ask = {**paused_state, "feedback_comment": "@forge ask how does this scale?"}
        assert route_task_plan_approval(state_ask) == "answer_question"

    def test_gate_routes_to_regenerate_plan_on_prefix(
        self, paused_state: TaskTakeoverState
    ) -> None:
        """Comment prefixed with '!' routes to regenerate_plan."""
        # 1. Direct bool flag
        state_bool = {**paused_state, "revision_requested": True}
        assert route_task_plan_approval(state_bool) == "regenerate_plan"

        # 2. '!' prefix comment
        state_excl = {**paused_state, "feedback_comment": "!Please add redis cache."}
        assert route_task_plan_approval(state_excl) == "regenerate_plan"

    def test_gate_routes_to_setup_workspace_on_label_approval(
        self, paused_state: TaskTakeoverState
    ) -> None:
        """Changing the label to forge:plan-approved clears is_paused and routes to setup_workspace."""
        state_approved = {**paused_state, "is_paused": False}
        assert route_task_plan_approval(state_approved) == "setup_workspace"

    def test_yolo_mode_bypasses_approval(self, paused_state: TaskTakeoverState) -> None:
        """YOLO mode bypasses the approval checkpoints completely."""
        state_yolo = {**paused_state, "yolo_mode": True}
        assert route_task_plan_approval(state_yolo) == "setup_workspace"


class TestWorkflowIdentityLabelTransitions:
    """Test that workflow identity labels are preserved across transitions."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "identity_label",
        ["forge:managed:task", "forge:managed:task-takeover"],
    )
    async def test_identity_labels_preserved_during_transition(self, identity_label: str) -> None:
        """Verify that forge:managed:task and forge:managed:task-takeover are not removed during transitions."""
        from forge.integrations.jira.client import JiraClient

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_client.put = AsyncMock(return_value=mock_response)

        # Initialize JiraClient and mock methods
        jira = JiraClient()
        jira._client = mock_client
        jira.get_labels = AsyncMock(
            return_value=[
                "forge:managed",
                identity_label,
                "forge:task-triage-pending",
            ]
        )

        with patch.object(jira, "_get_client", return_value=mock_client):
            await jira.set_workflow_label("TASK-123", ForgeLabel.PLAN_PENDING)

        # Retrieve the PUT request payload
        mock_client.put.assert_called_once()
        put_url = mock_client.put.call_args[0][0]
        put_json = mock_client.put.call_args[1]["json"]

        assert put_url == "/issue/TASK-123"

        # Verify operations
        operations = put_json["update"]["labels"]
        removed_labels = [op["remove"] for op in operations if "remove" in op]
        added_labels = [op["add"] for op in operations if "add" in op]

        # Verify that identity label was NOT removed
        assert identity_label not in removed_labels
        # Verify that the old state label was removed
        assert "forge:task-triage-pending" in removed_labels
        # Verify that the new plan pending label was added
        assert ForgeLabel.PLAN_PENDING.value in added_labels
