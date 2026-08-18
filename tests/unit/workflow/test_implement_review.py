"""Tests for the implement_review node and review_response_gate (proposal 007)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.graph import END

from forge.models.workflow import TicketType
from forge.workflow.bug.graph import build_bug_graph
from forge.workflow.feature.graph import build_feature_graph
from forge.workflow.task_takeover.graph import build_task_takeover_graph
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

    def test_human_review_gate_skips_pause_when_pr_merged(self):
        """human_review_gate must not re-pause when pr_merged is True."""
        from forge.workflow.nodes.human_review import human_review_gate

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            pr_merged=True,
        )
        result = human_review_gate(state)
        assert result["is_paused"] is False
        assert result["current_node"] == "human_review_gate"

    def test_human_review_gate_clears_stale_pause_when_pr_merged(self):
        """human_review_gate explicitly unpauses even if checkpoint had is_paused=True."""
        from forge.workflow.nodes.human_review import human_review_gate

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=True,
            pr_merged=True,
        )
        result = human_review_gate(state)
        assert result["is_paused"] is False

    def test_human_review_gate_pauses_when_pr_not_merged(self):
        """human_review_gate pauses normally when pr_merged is False."""
        from forge.workflow.nodes.human_review import human_review_gate

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=False,
            pr_merged=False,
        )
        result = human_review_gate(state)
        assert result["is_paused"] is True


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

    def test_review_response_gate_skips_pause_when_pr_merged(self):
        """review_response_gate must not re-pause when pr_merged is True."""
        from forge.workflow.nodes.implement_review import review_response_gate

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            pr_merged=True,
        )
        result = review_response_gate(state)
        assert result["is_paused"] is False
        assert result["current_node"] == "review_response_gate"

    def test_review_response_gate_clears_stale_pause_when_pr_merged(self):
        """review_response_gate explicitly unpauses even if checkpoint had is_paused=True."""
        from forge.workflow.nodes.implement_review import review_response_gate

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=True,
            pr_merged=True,
        )
        result = review_response_gate(state)
        assert result["is_paused"] is False

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


# ── gate→router regression: merge reaches completion path ─────────────────


class TestMergeReachesCompletionPath:
    """Gate→router integration: a merge event must flow through each review gate
    to the completion path, not get stuck at END due to stale is_paused."""

    def test_human_review_gate_merge_flows_to_complete_tasks(self):
        """human_review_gate(pr_merged) → route_human_review → complete_tasks."""
        from forge.workflow.nodes.human_review import (
            human_review_gate,
            route_human_review,
        )

        state = make_workflow_state(
            current_node="human_review_gate",
            is_paused=True,
            pr_merged=True,
        )
        gate_output = human_review_gate(state)
        assert route_human_review(gate_output) == "complete_tasks"

    def test_review_response_gate_merge_does_not_end(self):
        """review_response_gate(pr_merged) → route_review_response → not END."""
        from forge.workflow.nodes.implement_review import (
            review_response_gate,
            route_review_response,
        )

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=True,
            pr_merged=True,
        )
        gate_output = review_response_gate(state)
        assert route_review_response(gate_output) != END


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
        mock_runner.run = AsyncMock()

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


class TestThreadAwareReviewHandling:
    @pytest.mark.asyncio
    async def test_processed_threads_are_excluded_from_next_analysis(self):
        from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

        github = MagicMock()
        github.get_pull_request_review_threads = AsyncMock(
            return_value=[
                {
                    "thread_id": "already-accepted",
                    "path": "a.py",
                    "line": 1,
                    "comments": [{"comment_id": 10, "body": "Done", "author": "r"}],
                },
                {
                    "thread_id": "new-thread",
                    "path": "b.py",
                    "line": 2,
                    "comments": [{"comment_id": 20, "body": "New", "author": "r"}],
                },
            ]
        )
        github.close = AsyncMock()

        with patch("forge.workflow.nodes.implement_review.GitHubClient", return_value=github):
            result = await _fetch_pr_review_comments(
                "org",
                "repo",
                7,
                "",
                review_comments=[{"thread_id": "already-accepted", "disposition": "accept"}],
            )

        assert "already-accepted" not in result
        assert "new-thread" in result

    @pytest.mark.asyncio
    async def test_contested_thread_settled_while_bots_reply_is_last_comment(self):
        """A contested thread stays excluded as long as nothing new happened since Forge replied."""
        from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

        github = MagicMock()
        github.get_pull_request_review_threads = AsyncMock(
            return_value=[
                {
                    "thread_id": "contested-thread",
                    "path": "a.py",
                    "line": 1,
                    "comments": [
                        {
                            "comment_id": 10,
                            "body": "This is wrong",
                            "author": "reviewer",
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                        {
                            "comment_id": 11,
                            "body": "This conflicts with the public API.",
                            "author": "forge-bot",
                            "created_at": "2026-01-02T00:00:00Z",
                        },
                    ],
                },
            ]
        )
        github.close = AsyncMock()
        login_client = MagicMock()
        login_client.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
        login_client.close = AsyncMock()

        with patch(
            "forge.workflow.nodes.implement_review.GitHubClient",
            side_effect=[github, login_client],
        ):
            result = await _fetch_pr_review_comments(
                "org",
                "repo",
                7,
                "",
                review_comments=[{"thread_id": "contested-thread", "disposition": "contest"}],
            )

        assert "contested-thread" not in result

    @pytest.mark.asyncio
    async def test_contested_thread_resurfaces_once_human_replies_after_forge(self):
        """A human reply after Forge's objection re-opens the thread for re-analysis."""
        from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

        github = MagicMock()
        github.get_pull_request_review_threads = AsyncMock(
            return_value=[
                {
                    "thread_id": "contested-thread",
                    "path": "a.py",
                    "line": 1,
                    "comments": [
                        {
                            "comment_id": 10,
                            "body": "This is wrong",
                            "author": "reviewer",
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                        {
                            "comment_id": 11,
                            "body": "This conflicts with the public API.",
                            "author": "forge-bot",
                            "created_at": "2026-01-02T00:00:00Z",
                        },
                        {
                            "comment_id": 12,
                            "body": "Please do it anyway.",
                            "author": "reviewer",
                            "created_at": "2026-01-03T00:00:00Z",
                        },
                    ],
                },
            ]
        )
        github.close = AsyncMock()
        login_client = MagicMock()
        login_client.get_authenticated_user = AsyncMock(return_value={"login": "forge-bot"})
        login_client.close = AsyncMock()

        with patch(
            "forge.workflow.nodes.implement_review.GitHubClient",
            side_effect=[github, login_client],
        ):
            result = await _fetch_pr_review_comments(
                "org",
                "repo",
                7,
                "",
                review_comments=[{"thread_id": "contested-thread", "disposition": "contest"}],
            )

        assert "contested-thread" in result
        assert "Please do it anyway." in result

    @pytest.mark.asyncio
    async def test_reply_to_review_threads_uses_skip_addressed_guard(self):
        """A second guard: never re-post to a decision already marked addressed."""
        from forge.workflow.nodes.implement_review import _reply_to_review_threads

        with patch(
            "forge.workflow.nodes.implement_review.reply_to_review_decisions",
            new=AsyncMock(),
        ) as reply_mock:
            await _reply_to_review_threads(
                owner="org", repo="repo", pr_number=9, decisions=[{"thread_id": "t"}]
            )

        reply_mock.assert_awaited_once_with(
            repo_full_name="org/repo",
            pr_number=9,
            decisions=[{"thread_id": "t"}],
            skip_addressed=True,
        )

    @pytest.mark.asyncio
    async def test_no_bot_login_lookup_when_no_contested_decisions(self):
        """Avoid the extra GET /user round trip when there's nothing to settle-check."""
        from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

        github = MagicMock()
        github.get_pull_request_review_threads = AsyncMock(
            return_value=[
                {
                    "thread_id": "new-thread",
                    "path": "b.py",
                    "line": 2,
                    "comments": [
                        {
                            "comment_id": 20,
                            "body": "New",
                            "author": "r",
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
            ]
        )
        github.close = AsyncMock()

        with patch(
            "forge.workflow.nodes.implement_review.GitHubClient", return_value=github
        ) as ctor:
            result = await _fetch_pr_review_comments("org", "repo", 7, "", review_comments=[])

        ctor.assert_called_once()
        assert "new-thread" in result

    @pytest.mark.asyncio
    async def test_contested_thread_resurfaces_when_bot_login_lookup_fails(self):
        """A failed identity lookup must fail toward re-analysis, not a coincidental
        empty-string match against a deleted account's comment author."""
        from forge.workflow.nodes.implement_review import _fetch_pr_review_comments

        github = MagicMock()
        github.get_pull_request_review_threads = AsyncMock(
            return_value=[
                {
                    "thread_id": "contested-thread",
                    "path": "a.py",
                    "line": 1,
                    "comments": [
                        {
                            "comment_id": 10,
                            "body": "This is wrong",
                            "author": "reviewer",
                            "created_at": "2026-01-01T00:00:00Z",
                        },
                        {
                            "comment_id": 11,
                            "body": "This conflicts with the public API.",
                            "author": "",  # e.g. a deleted account, per GitHub's API
                            "created_at": "2026-01-02T00:00:00Z",
                        },
                    ],
                },
            ]
        )
        github.close = AsyncMock()
        login_client = MagicMock()
        login_client.get_authenticated_user = AsyncMock(side_effect=RuntimeError("auth failed"))
        login_client.close = AsyncMock()

        with patch(
            "forge.workflow.nodes.implement_review.GitHubClient",
            side_effect=[github, login_client],
        ):
            result = await _fetch_pr_review_comments(
                "org",
                "repo",
                7,
                "",
                review_comments=[{"thread_id": "contested-thread", "disposition": "contest"}],
            )

        assert "contested-thread" in result

    @pytest.mark.asyncio
    async def test_legacy_objections_file_still_pauses_for_response(self, tmp_path):
        from forge.workflow.nodes.implement_review import implement_review

        async def run_container(**_kwargs):
            (tmp_path / ".forge" / "review-objections.md").write_text("Legacy objection")
            (tmp_path / ".forge" / "review-plan.md").write_text("# No actionable items")

        runner = MagicMock()
        runner.run = AsyncMock(side_effect=run_container)
        git = MagicMock()
        git._run_git.return_value = MagicMock(stdout="")
        state = make_workflow_state(
            ticket_key="TEST-233",
            current_node="implement_review",
            workspace_path=str(tmp_path),
            current_repo="org/repo",
            current_pr_number=9,
            feedback_comment="Review",
            context={"branch_name": "forge/TEST-233"},
        )

        with (
            patch(
                "forge.workflow.nodes.implement_review.prepare_workspace",
                return_value=(str(tmp_path), git),
            ),
            patch(
                "forge.workflow.nodes.implement_review._fetch_pr_review_comments",
                new=AsyncMock(return_value="# Review"),
            ),
            patch(
                "forge.workflow.nodes.implement_review._post_review_addressing_comment",
                new=AsyncMock(),
            ),
            patch(
                "forge.workflow.nodes.implement_review._post_review_objection",
                new=AsyncMock(),
            ) as post_objection,
            patch(
                "forge.workflow.nodes.implement_review.ContainerRunner",
                return_value=runner,
            ),
        ):
            result = await implement_review(state)

        post_objection.assert_awaited_once()
        assert result["current_node"] == "review_response_gate"
        assert result["contested_comments"] == [{"text": "Legacy objection"}]

    @pytest.mark.asyncio
    async def test_contested_thread_does_not_block_accepted_plan(self, tmp_path):
        from forge.workflow.nodes.implement_review import implement_review

        decisions = [
            {
                "thread_id": "accepted-thread",
                "comment_id": 10,
                "disposition": "accept",
                "feedback": "Handle the empty case.",
                "reason": "Valid edge case",
                "response": "",
            },
            {
                "thread_id": "contested-thread",
                "comment_id": 20,
                "disposition": "contest",
                "feedback": "",
                "reason": "Conflicts with the public API",
                "response": "This conflicts with the documented public API. Can you confirm?",
            },
        ]

        async def run_container(**_kwargs):
            if not (tmp_path / ".forge" / "review-decisions.json").exists():
                (tmp_path / ".forge" / "review-decisions.json").write_text(json.dumps(decisions))
                (tmp_path / ".forge" / "review-plan.md").write_text(
                    "# Plan\n\nImplement accepted-thread."
                )

        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(side_effect=run_container)
        mock_git = MagicMock()
        mock_git.has_uncommitted_changes.return_value = False
        mock_git._run_git.return_value = MagicMock(stdout="")
        state = make_workflow_state(
            ticket_key="TEST-233",
            current_node="implement_review",
            workspace_path=str(tmp_path),
            current_repo="org/repo",
            current_pr_number=9,
            feedback_comment="Mixed review",
            context={"branch_name": "forge/TEST-233"},
        )

        with (
            patch(
                "forge.workflow.nodes.implement_review.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch(
                "forge.workflow.nodes.implement_review._fetch_pr_review_comments",
                new=AsyncMock(return_value="# Review"),
            ),
            patch(
                "forge.workflow.nodes.implement_review._post_review_addressing_comment",
                new=AsyncMock(),
            ),
            patch(
                "forge.workflow.nodes.implement_review._reply_to_review_threads",
                new=AsyncMock(),
            ) as reply_threads,
            patch(
                "forge.workflow.nodes.implement_review.ContainerRunner",
                return_value=mock_runner,
            ),
        ):
            result = await implement_review(state)

        assert mock_runner.run.await_count == 2
        assert result["current_node"] == "review_response_gate"
        # Once Forge has replied to the contested thread, it's marked addressed
        # so the skip_addressed guard in reply_to_review_decisions can catch a
        # stray re-reply if the same decision ever resurfaces unchanged.
        assert result["contested_comments"] == [{**decisions[1], "status": "addressed"}]
        assert result["review_comments"][0] == {
            **decisions[0],
            "response": "Forge verified this feedback; no additional code change was needed.",
        }
        assert result["review_comments"][1] == {**decisions[1], "status": "addressed"}
        assert reply_threads.await_count == 2

    @pytest.mark.asyncio
    async def test_contested_decision_marked_addressed_after_reply(self, tmp_path):
        """Once Forge has replied to a contested thread, the persisted decision
        carries status=addressed, so a decision that somehow re-surfaces
        unchanged is skipped by reply_to_review_decisions' skip_addressed guard."""
        from forge.workflow.nodes.implement_review import implement_review

        decisions = [
            {
                "thread_id": "contested-thread",
                "comment_id": 20,
                "disposition": "contest",
                "feedback": "",
                "reason": "Conflicts with the public API",
                "response": "This conflicts with the documented public API. Can you confirm?",
            },
        ]

        async def run_container(**_kwargs):
            if not (tmp_path / ".forge" / "review-decisions.json").exists():
                (tmp_path / ".forge" / "review-decisions.json").write_text(json.dumps(decisions))
                (tmp_path / ".forge" / "review-plan.md").write_text("# No actionable items")

        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(side_effect=run_container)
        mock_git = MagicMock()
        mock_git.has_uncommitted_changes.return_value = False
        mock_git._run_git.return_value = MagicMock(stdout="")
        state = make_workflow_state(
            ticket_key="TEST-234",
            current_node="implement_review",
            workspace_path=str(tmp_path),
            current_repo="org/repo",
            current_pr_number=9,
            feedback_comment="Review",
            context={"branch_name": "forge/TEST-234"},
        )

        with (
            patch(
                "forge.workflow.nodes.implement_review.prepare_workspace",
                return_value=(str(tmp_path), mock_git),
            ),
            patch(
                "forge.workflow.nodes.implement_review._fetch_pr_review_comments",
                new=AsyncMock(return_value="# Review"),
            ),
            patch(
                "forge.workflow.nodes.implement_review._post_review_addressing_comment",
                new=AsyncMock(),
            ),
            patch(
                "forge.workflow.nodes.implement_review._reply_to_review_threads",
                new=AsyncMock(),
            ),
            patch(
                "forge.workflow.nodes.implement_review.ContainerRunner",
                return_value=mock_runner,
            ),
        ):
            result = await implement_review(state)

        assert result["review_comments"][0]["status"] == "addressed"

    def test_confirming_one_thread_routes_to_implementation_with_others_pending(self):
        from forge.workflow.nodes.implement_review import route_review_response

        state = make_workflow_state(
            current_node="review_response_gate",
            is_paused=False,
            revision_requested=True,
            contested_comments=[{"thread_id": "still-pending", "comment_id": 20}],
        )

        assert route_review_response(state) == "implement_review"


# ── resume path from review_response_gate after forge:retry ────────────────────


class TestResumeFromReviewResponseGateAfterRetry:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ticket_type, graph_builder",
        [
            (TicketType.FEATURE, build_feature_graph),
            (TicketType.BUG, build_bug_graph),
            (TicketType.TASK, build_task_takeover_graph),
        ],
    )
    async def test_graph_level_resume_from_review_response_gate_after_retry(
        self, ticket_type: TicketType, graph_builder: Any
    ) -> None:
        """Verify that a resumed workflow starts fresh and pauses at human_review_gate."""
        # 1. Compile the graph
        compiled_graph = graph_builder().compile()

        # 2. Setup initial state representing the state AFTER worker handles forge:retry.
        # The worker transitions current_node to "human_review_gate" and clears review variables.
        initial_state = make_workflow_state(
            ticket_key="TEST-123",
            ticket_type=ticket_type,
            current_node="human_review_gate",
            is_paused=False,  # Worker resumes execution
            contested_comments=[],  # Cleared by worker
            revision_requested=False,  # Cleared by worker
            feedback_comment=None,  # Cleared by worker
            context={"force_fresh_invoke": True},
        )

        # 3. Invoke the graph
        result_state = await compiled_graph.ainvoke(initial_state)

        # 4. Assertions:
        # Verify that the graph routed into the human_review_gate node,
        # executed it, and paused the workflow there, clearing any in-flight review state.
        assert result_state["current_node"] == "human_review_gate"
        assert result_state["is_paused"] is True
        assert result_state["contested_comments"] == []
        assert result_state["revision_requested"] is False
        assert result_state["feedback_comment"] is None
