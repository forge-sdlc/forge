"""Tests for the updated human_review_gate and route_human_review."""

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

BASE_STATE = {
    "ticket_key": "TEST-1",
    "pr_urls": ["https://github.com/org/repo/pull/1"],
    "current_pr_number": 42,
    "is_paused": False,
    "pending_ci_event": False,
    "ci_status": None,
    "revision_requested": False,
    "pr_merged": False,
    "feedback_comment": None,
}


class TestRouteHumanReview:
    def test_pending_ci_event_routes_to_ci_evaluator(self):
        """pending_ci_event=True routes to ci_evaluator before all other checks."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {**BASE_STATE, "pending_ci_event": True, "is_paused": True}
        assert route_human_review(state) == "ci_evaluator"

    def test_pending_ci_event_overrides_revision_requested(self):
        """CI event takes priority even if revision is also pending."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            **BASE_STATE,
            "pending_ci_event": True,
            "revision_requested": True,
            "feedback_comment": "please fix",
            "is_paused": False,
        }
        assert route_human_review(state) == "ci_evaluator"

    def test_pr_merged_takes_priority_over_pending_ci_event(self):
        """A merge takes priority even when pending_ci_event is also set —
        route_human_review must not send an already-merged PR through
        ci_evaluator just because a stale/racing CI event is pending."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            **BASE_STATE,
            "pending_ci_event": True,
            "pr_merged": True,
        }
        assert route_human_review(state) == "complete_tasks"

    def test_no_ci_event_paused_returns_end(self):
        """With no pending CI event, paused=True returns END."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {**BASE_STATE, "pending_ci_event": False, "is_paused": True}
        assert route_human_review(state) == END

    def test_revision_requested_routes_to_implement_review(self):
        """review feedback routes to implement_review when not paused."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {
            **BASE_STATE,
            "pending_ci_event": False,
            "is_paused": False,
            "revision_requested": True,
            "feedback_comment": "please refactor",
        }
        assert route_human_review(state) == "implement_review"

    def test_pr_merged_routes_to_complete_tasks(self):
        """pr_merged=True routes to complete_tasks."""
        from forge.workflow.nodes.human_review import route_human_review

        state = {**BASE_STATE, "pending_ci_event": False, "is_paused": False, "pr_merged": True}
        assert route_human_review(state) == "complete_tasks"


class TestHumanReviewGate:
    @pytest.mark.asyncio
    @patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions",
        new_callable=AsyncMock,
    )
    async def test_initial_entry_posts_comment_and_updates_labels(self, persist):
        """On initial entry (ci_status=None), gate posts comment and updates labels."""
        from forge.workflow.nodes.human_review import human_review_gate

        state = {**BASE_STATE, "ci_status": None, "pending_ci_event": False}
        result = await human_review_gate(state)

        persist.assert_awaited_once()
        actions = persist.await_args.args[1]
        comment_text = actions[0].payload["body"]
        assert "42" in comment_text  # PR number in comment
        assert [action.operation for action in actions] == [
            "jira.comment.create",
            "jira.labels.remove",
            "jira.label.set",
        ]
        assert result["is_paused"] is True
        assert result["current_node"] == "human_review_gate"
        assert result["pr_created_comment_posted"] is True

    @pytest.mark.asyncio
    @patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions",
        new_callable=AsyncMock,
    )
    async def test_subsequent_entry_skips_comment(self, persist):
        """On re-entry (ci_status already set), gate skips Jira comment."""
        from forge.workflow.nodes.human_review import human_review_gate

        state = {**BASE_STATE, "ci_status": "pending", "pending_ci_event": False}
        result = await human_review_gate(state)

        persist.assert_not_awaited()
        assert result["is_paused"] is True

    @pytest.mark.asyncio
    @patch(
        "forge.workflow.nodes.human_review.execute_persistence_actions",
        new_callable=AsyncMock,
    )
    async def test_first_ci_webhook_reentry_does_not_repost_comment(self, persist):
        """The first CI webhook re-enters the gate while ci_status is still None.

        ci_evaluator has not run yet, so the guard must rely on
        pr_created_comment_posted (not ci_status) to avoid a duplicate
        'Pull request created' Jira comment.
        """
        from forge.workflow.nodes.human_review import human_review_gate

        state = {
            **BASE_STATE,
            "ci_status": None,
            "pending_ci_event": True,
            "pr_created_comment_posted": True,
        }
        result = await human_review_gate(state)

        persist.assert_not_awaited()
        assert result["is_paused"] is True
