"""Tests for the shared code_review utility module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.fixtures.workflow_states import make_workflow_state

FIX_COMMITS = (
    "Fix CalculateJitteredDuration to use positive-only jitter\n"
    "Change from symmetric [-10%, +10%] to [0%, +20%]"
)


# ── run_post_change_review ────────────────────────────────────────────────────


class TestRunPostChangeReview:
    """run_post_change_review runs local-review container and commits fixes."""

    @pytest.mark.asyncio
    async def test_commits_review_fixes_when_changes_exist(self):
        """Returns True when the container leaves uncommitted changes."""
        from forge.workflow.nodes.code_review import run_post_change_review

        git_mock = MagicMock()
        git_mock.has_uncommitted_changes.return_value = True
        git_mock.stage_all = MagicMock()
        git_mock.commit = MagicMock()

        runner_mock = MagicMock()
        runner_mock.run = AsyncMock()

        with patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock), \
             patch("forge.workflow.nodes.code_review.GitOperations", return_value=git_mock), \
             patch("forge.workflow.nodes.code_review.Workspace"), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            result = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
                label="ci-fix-1",
            )

        assert result is True
        git_mock.stage_all.assert_called_once()
        git_mock.commit.assert_called_once()
        assert "ci-fix-1" in git_mock.commit.call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_false_when_no_changes(self):
        """Returns False when the container made no changes."""
        from forge.workflow.nodes.code_review import run_post_change_review

        git_mock = MagicMock()
        git_mock.has_uncommitted_changes.return_value = False

        runner_mock = MagicMock()
        runner_mock.run = AsyncMock()

        with patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock), \
             patch("forge.workflow.nodes.code_review.GitOperations", return_value=git_mock), \
             patch("forge.workflow.nodes.code_review.Workspace"), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            result = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
            )

        assert result is False
        git_mock.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_container_error_does_not_propagate(self):
        """Container failure returns False and does not raise."""
        from forge.workflow.nodes.code_review import run_post_change_review

        runner_mock = MagicMock()
        runner_mock.run = AsyncMock(side_effect=RuntimeError("container crashed"))

        with patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            result = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
            )

        assert result is False


# ── sync_pr_description ───────────────────────────────────────────────────────


def _git_mock(commit_log: str = FIX_COMMITS) -> MagicMock:
    git = MagicMock()
    git._run_git.return_value.stdout = commit_log
    return git


def _github_jira_mocks(pr_body: str):
    github = MagicMock()
    github.get_pull_request = AsyncMock(return_value={"body": pr_body, "number": 42})
    github.update_pull_request = AsyncMock(return_value={"number": 42})
    github.close = AsyncMock()

    jira = MagicMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    return github, jira


class TestSyncPrDescription:
    """sync_pr_description updates the PR body when commits contradict it."""

    @pytest.fixture
    def state(self):
        return make_workflow_state(ticket_key="TEST-123")

    @pytest.mark.asyncio
    async def test_updates_pr_when_description_is_inaccurate(self, state):
        """Agent-returned updated body is patched to the PR and Jira notified."""
        from forge.workflow.nodes.code_review import sync_pr_description

        original = "The jitter is +-10% uniform."
        updated = "The jitter is [0%, +20%] positive-only."
        github, jira = _github_jira_mocks(original)

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value=updated)
        agent_mock.close = AsyncMock()
        agent_mock._strip_preamble = MagicMock(side_effect=lambda x: x)

        with patch("forge.workflow.nodes.code_review.GitHubClient", return_value=github), \
             patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira), \
             patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            await sync_pr_description(
                state, _git_mock(),
                owner="org", repo="repo", pr_number=42, attempt=2,
            )

        github.update_pull_request.assert_called_once_with("org", "repo", 42, body=updated)
        jira.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_body_unchanged(self, state):
        """No PR update or Jira comment when the body is already accurate."""
        from forge.workflow.nodes.code_review import sync_pr_description

        body = "The jitter is +-10% uniform."
        github, jira = _github_jira_mocks(body)

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value=body)
        agent_mock.close = AsyncMock()
        agent_mock._strip_preamble = MagicMock(side_effect=lambda x: x)

        with patch("forge.workflow.nodes.code_review.GitHubClient", return_value=github), \
             patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira), \
             patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            await sync_pr_description(
                state, _git_mock(),
                owner="org", repo="repo", pr_number=42, attempt=2,
            )

        github.update_pull_request.assert_not_called()
        jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_commits(self, state):
        """Empty commit log skips the agent call entirely."""
        from forge.workflow.nodes.code_review import sync_pr_description

        github, jira = _github_jira_mocks("body")

        with patch("forge.workflow.nodes.code_review.GitHubClient", return_value=github), \
             patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira), \
             patch("forge.workflow.nodes.code_review.ForgeAgent") as MockAgent:
            await sync_pr_description(
                state, _git_mock(""),
                owner="org", repo="repo", pr_number=42, attempt=1,
            )

        MockAgent.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_pr_number(self, state):
        """No PR number means nothing to update."""
        from forge.workflow.nodes.code_review import sync_pr_description

        with patch("forge.workflow.nodes.code_review.GitHubClient") as MockGH:
            await sync_pr_description(
                state, MagicMock(),
                owner="org", repo="repo", pr_number=None, attempt=1,
            )

        MockGH.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_does_not_propagate(self, state):
        """Agent failure never blocks the caller."""
        from forge.workflow.nodes.code_review import sync_pr_description

        github, jira = _github_jira_mocks("body")

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(side_effect=RuntimeError("timeout"))
        agent_mock.close = AsyncMock()

        with patch("forge.workflow.nodes.code_review.GitHubClient", return_value=github), \
             patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira), \
             patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            await sync_pr_description(
                state, _git_mock(),
                owner="org", repo="repo", pr_number=42, attempt=1,
            )

        github.update_pull_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_comment_labels_initial_create(self, state):
        """attempt=0 produces a human-readable 'PR creation' label in the comment."""
        from forge.workflow.nodes.code_review import sync_pr_description

        github, jira = _github_jira_mocks("old body")

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value="new body")
        agent_mock.close = AsyncMock()

        with patch("forge.workflow.nodes.code_review.GitHubClient", return_value=github), \
             patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira), \
             patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock), \
             patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"):
            await sync_pr_description(
                state, _git_mock(),
                owner="org", repo="repo", pr_number=42, attempt=0,
            )

        comment_text = jira.add_comment.call_args[0][1]
        assert "PR creation" in comment_text


# ── integration: sync wired into create_pull_request ─────────────────────────


class TestSyncCalledFromCreatePR:
    """sync_pr_description is called by create_pull_request after PR creation."""

    @pytest.mark.asyncio
    async def test_sync_called_after_pr_creation(self):
        from forge.workflow.nodes.pr_creation import create_pull_request

        state = make_workflow_state(
            current_node="create_pr",
            current_repo="org/repo",
            implemented_tasks=["TEST-200"],
            workspace_path="/tmp/forge-workspace-test",
            context={"branch_name": "forge/test-123"},
        )

        mock_github = MagicMock()
        mock_github.get_or_create_fork = AsyncMock(
            return_value={"owner": {"login": "fork-user"}, "name": "repo"}
        )
        mock_github.sync_fork_with_upstream = AsyncMock()
        mock_github.add_fork_remote = MagicMock()
        mock_github.create_pull_request = AsyncMock(
            return_value={"number": 42, "html_url": "https://github.com/org/repo/pull/42"}
        )
        mock_github.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.get_issue = AsyncMock(return_value=MagicMock(summary="Test feature"))
        mock_jira.add_comment = AsyncMock()
        mock_jira.create_remote_link = AsyncMock()
        mock_jira.close = AsyncMock()

        mock_git = MagicMock()
        mock_git.push_to_fork = MagicMock()
        mock_git.add_fork_remote = MagicMock()

        with patch("forge.workflow.nodes.pr_creation.GitHubClient", return_value=mock_github), \
             patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira), \
             patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git), \
             patch("forge.workflow.nodes.pr_creation.Workspace"), \
             patch("forge.workflow.nodes.pr_creation.check_merge_conflicts",
                   AsyncMock(return_value=(False, []))), \
             patch("forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
                   AsyncMock(return_value="## Summary\n\nTest PR.")), \
             patch("forge.workflow.nodes.pr_creation.sync_pr_description",
                   new_callable=AsyncMock) as mock_sync:
            await create_pull_request(state)

        mock_sync.assert_called_once()
        _, call_kwargs = mock_sync.call_args
        assert call_kwargs.get("attempt") == 0
