"""Tests for the implement_review node and review_response_gate (proposal 007)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from tests.fixtures.workflow_states import make_workflow_state

# ── State fields ──────────────────────────────────────────────────────────────


class TestReviewStateFields:
    def test_review_comments_in_review_integration_state(self):
        """review_comments must be a field in ReviewIntegrationState."""
        from forge.workflow.base import ReviewIntegrationState

        assert "review_comments" in ReviewIntegrationState.__annotations__

    def test_contested_comments_in_review_integration_state(self):
        from forge.workflow.base import ReviewIntegrationState

        assert "contested_comments" in ReviewIntegrationState.__annotations__

    def test_review_response_posted_in_review_integration_state(self):
        from forge.workflow.base import ReviewIntegrationState

        assert "review_response_posted" in ReviewIntegrationState.__annotations__

    def test_initial_feature_state_has_empty_review_fields(self):
        from forge.models.workflow import TicketType
        from forge.workflow.feature.state import create_initial_feature_state

        state = create_initial_feature_state(
            thread_id="t", ticket_key="TEST-1", ticket_type=TicketType.FEATURE
        )
        assert state.get("review_comments") == []
        assert state.get("contested_comments") == []
        assert state.get("review_response_posted") is False


# ── route_human_review routes to implement_review on changes_requested ────────


class TestHumanReviewRoutingToImplementReview:
    def test_changes_requested_routes_to_implement_review_not_implement_task(self):
        """On changes_requested, route to implement_review, not implement_task."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            revision_requested=True,
            feedback_comment="The session token must be HMAC-signed.",
        )
        assert route_human_review(state) == "implement_review"

    def test_merged_still_routes_to_complete_tasks(self):
        """PR merged still goes to complete_tasks."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            pr_merged=True,
        )
        assert route_human_review(state) == "complete_tasks"

    def test_paused_still_routes_to_end(self):
        """Waiting for review still returns END."""
        from forge.workflow.nodes.human_review import route_human_review

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=True,
        )
        assert route_human_review(state) == END


# ── review_response_gate pause node ──────────────────────────────────────────


class TestReviewResponseGate:
    def test_review_response_gate_pauses_workflow(self):
        """review_response_gate sets is_paused=True."""
        from forge.workflow.nodes.implement_review import review_response_gate

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
        )
        result = review_response_gate(state)
        assert result["is_paused"] is True
        assert result["current_node"] == "review_response_gate"

    def test_route_review_response_confirmed_resumes_implement_review(self):
        """When human confirms, route back to implement_review.

        Worker sets revision_requested=True (confirmed) and clears
        contested_comments so the agent knows to implement this time.
        """
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            revision_requested=True,  # human confirmed — implement it
            contested_comments=[],  # cleared by worker
        )
        assert route_review_response(state) == "implement_review"

    def test_route_review_response_withdrawn_routes_to_human_review_gate(self):
        """When human withdraws the request, route back to human_review_gate."""
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            revision_requested=False,
            pr_merged=False,
            feedback_comment=None,
        )
        # No revision_requested and no contested → human withdrew
        assert route_review_response(state) == "human_review_gate"

    def test_route_review_response_paused_returns_end(self):
        """Still waiting for human response → END."""
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=True,
        )
        assert route_review_response(state) == END


# ── implement_review in feature graph ────────────────────────────────────────


class TestImplementReviewInFeatureGraph:
    def test_implement_review_is_a_node(self):
        """implement_review must be a node in the feature graph."""
        from forge.workflow.feature.graph import build_feature_graph

        graph = build_feature_graph()
        compiled = graph.compile()
        assert "implement_review" in compiled.get_graph().nodes

    def test_review_response_gate_is_a_node(self):
        """review_response_gate must be a node in the feature graph."""
        from forge.workflow.feature.graph import build_feature_graph

        graph = build_feature_graph()
        compiled = graph.compile()
        assert "review_response_gate" in compiled.get_graph().nodes

    def test_human_review_gate_has_implement_review_edge(self):
        """human_review_gate must have an edge to implement_review."""
        from forge.workflow.feature.graph import build_feature_graph

        graph = build_feature_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "human_review_gate"}
        assert "implement_review" in targets

    def test_implement_task_not_reachable_from_human_review_gate(self):
        """implement_task must NOT be a direct target of human_review_gate."""
        from forge.workflow.feature.graph import build_feature_graph

        graph = build_feature_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "human_review_gate"}
        assert "implement_task" not in targets


# ── implement_review in bug graph ────────────────────────────────────────────


class TestImplementReviewInBugGraph:
    def test_implement_review_is_a_node_in_bug_graph(self):
        from forge.workflow.bug.graph import build_bug_graph

        graph = build_bug_graph()
        compiled = graph.compile()
        assert "implement_review" in compiled.get_graph().nodes

    def test_human_review_gate_routes_to_implement_review_in_bug_graph(self):
        from forge.workflow.bug.graph import build_bug_graph

        graph = build_bug_graph()
        compiled = graph.compile()
        targets = {e.target for e in compiled.get_graph().edges if e.source == "human_review_gate"}
        assert "implement_review" in targets


# ── resume routing ────────────────────────────────────────────────────────────


class TestResumeRoutingForReviewNodes:
    def test_feature_resumes_at_implement_review(self):
        from forge.workflow.feature.graph import route_by_ticket_type

        state = make_workflow_state(current_node="implement_review")
        assert route_by_ticket_type(state) == "implement_review"

    def test_feature_resumes_at_review_response_gate(self):
        from forge.workflow.feature.graph import route_by_ticket_type

        state = make_workflow_state(current_node="review_response_gate")
        assert route_by_ticket_type(state) == "review_response_gate"

    def test_bug_resumes_at_implement_review(self):
        from forge.workflow.bug.graph import route_entry

        state = make_workflow_state(current_node="implement_review")
        assert route_entry(state) == "implement_review"

    def test_bug_resumes_at_review_response_gate(self):
        from forge.workflow.bug.graph import route_entry

        state = make_workflow_state(current_node="review_response_gate")
        assert route_entry(state) == "review_response_gate"


# ── implement_review error handling ──────────────────────────────────────────


class TestImplementReviewErrorHandling:
    @pytest.mark.asyncio
    async def test_workspace_prepare_failure_increments_retry_count(self):
        """ValueError from prepare_workspace increments retry_count."""
        from forge.workflow.nodes.implement_review import implement_review

        state = make_workflow_state(
            current_node="implement_review",
            retry_count=1,
            workspace_path="/tmp/workspace",
            current_repo="org/repo",
            feedback_comment="Fix the tests",
        )

        with patch(
            "forge.workflow.nodes.implement_review.prepare_workspace",
            side_effect=ValueError("workspace gone"),
        ):
            result = await implement_review(state)

        assert result["current_node"] == "implement_review"
        assert result["retry_count"] == 2
        assert "workspace gone" in result["last_error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("plan_contents", [None, ""])
    async def test_missing_or_empty_review_plan_is_an_analysis_failure(
        self, tmp_path, plan_contents
    ):
        """Analysis must fail closed when the required plan artifact is invalid."""
        from forge.workflow.nodes.implement_review import implement_review

        mock_git = MagicMock()
        mock_runner = MagicMock()

        async def run_analysis(**_kwargs):
            if plan_contents is not None:
                plan_path = tmp_path / ".forge" / "review-plan.md"
                plan_path.write_text(plan_contents)

        mock_runner.run = AsyncMock(side_effect=run_analysis)
        state = make_workflow_state(
            current_node="implement_review",
            retry_count=1,
            workspace_path=str(tmp_path),
            current_repo="org/repo",
            feedback_comment="Fix the tests",
            current_pr_number=17,
        )

        with (
            patch(
                "forge.workflow.nodes.implement_review.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch(
                "forge.workflow.nodes.implement_review._post_review_addressing_comment",
                new=AsyncMock(),
            ),
            patch(
                "forge.workflow.nodes.implement_review._fetch_pr_review_comments",
                new=AsyncMock(return_value="# PR Review Feedback\n"),
            ),
            patch(
                "forge.workflow.nodes.implement_review.ContainerRunner",
                return_value=mock_runner,
            ),
        ):
            result = await implement_review(state)

        assert result["current_node"] == "implement_review"
        assert result["retry_count"] == 2
        assert "review-plan.md" in result["last_error"]


class TestReviewPlanValidation:
    def test_plan_without_actionable_section_is_a_valid_noop(self):
        from forge.workflow.nodes.implement_review import (
            _review_plan_has_actionable_items,
        )

        plan = """# Review Plan

## Acknowledged (not addressed)

### Intentional behavior

No change required.
"""

        assert _review_plan_has_actionable_items(plan) is False

    def test_plan_with_actionable_item_requires_implementation(self):
        from forge.workflow.nodes.implement_review import (
            _review_plan_has_actionable_items,
        )

        plan = """# Review Plan

## Actionable Items

### Item 1: Fix retry handling

**Change:** Fail closed.
"""

        assert _review_plan_has_actionable_items(plan) is True

    @pytest.mark.parametrize(
        "plan",
        [
            "# No actionable items",
            "# Review Plan\n\n## Actionable Items\n\nNothing listed.",
        ],
    )
    def test_malformed_plan_is_rejected(self, plan):
        from forge.workflow.nodes.implement_review import (
            _review_plan_has_actionable_items,
        )

        with pytest.raises(ValueError):
            _review_plan_has_actionable_items(plan)


class TestImplementReviewStatusComment:
    @pytest.mark.asyncio
    async def test_posts_addressing_review_comment_when_review_work_starts(self, tmp_path):
        """implement_review posts an informational PR status when work starts."""
        from forge.workflow.nodes.implement_review import (
            _REVIEW_ADDRESSING_COMMENT,
            implement_review,
        )

        mock_git = MagicMock()
        mock_git._run_git.return_value = MagicMock(stdout="")
        mock_github = MagicMock()
        mock_github.create_issue_comment = AsyncMock()
        mock_github.close = AsyncMock()
        mock_runner = MagicMock()

        async def write_no_action_plan(**_kwargs):
            plan_path = tmp_path / ".forge" / "review-plan.md"
            plan_path.write_text(
                "# Review Plan\n\n"
                "## Acknowledged (not addressed)\n\n"
                "### No changes requested\n\n"
                "The feedback requires no code changes.\n"
            )

        mock_runner.run = AsyncMock(side_effect=write_no_action_plan)

        state = make_workflow_state(
            current_node="implement_review",
            ticket_key="TEST-456",
            workspace_path=str(tmp_path),
            current_repo="org/repo",
            feedback_comment="Please simplify this flow.",
            current_pr_number=17,
            context={"branch_name": "forge/TEST-456"},
        )

        with (
            patch(
                "forge.workflow.nodes.implement_review.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch(
                "forge.workflow.nodes.implement_review._fetch_pr_review_comments",
                new=AsyncMock(return_value="# PR Review Feedback\n"),
            ),
            patch("forge.workflow.nodes.implement_review.GitHubClient", return_value=mock_github),
            patch(
                "forge.workflow.nodes.implement_review.ContainerRunner", return_value=mock_runner
            ),
        ):
            result = await implement_review(state)

        mock_github.create_issue_comment.assert_called_once_with(
            "org",
            "repo",
            17,
            _REVIEW_ADDRESSING_COMMENT,
        )
        mock_github.close.assert_called_once()
        mock_runner.run.assert_called_once()
        assert result["current_node"] == "human_review_gate"

    @pytest.mark.asyncio
    async def test_skips_addressing_review_comment_without_pr_number(self):
        """No PR comment is posted if the workflow has no PR number."""
        from forge.workflow.nodes.implement_review import _post_review_addressing_comment

        mock_github = MagicMock()
        mock_github.create_issue_comment = AsyncMock()

        with patch("forge.workflow.nodes.implement_review.GitHubClient", return_value=mock_github):
            await _post_review_addressing_comment(
                ticket_key="TEST-789",
                owner="org",
                repo="repo",
                pr_number=None,
            )

        mock_github.create_issue_comment.assert_not_called()
