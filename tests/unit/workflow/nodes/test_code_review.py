"""Tests for the shared code_review utility module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.observability.review_poller import ReviewCycleData
from forge.sandbox.runner import ContainerResult
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

        with (
            patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock),
            patch("forge.workflow.nodes.code_review.GitOperations", return_value=git_mock),
            patch("forge.workflow.nodes.code_review.Workspace"),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            committed, _ = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
                label="ci-fix-1",
            )

        assert committed is True
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

        with (
            patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock),
            patch("forge.workflow.nodes.code_review.GitOperations", return_value=git_mock),
            patch("forge.workflow.nodes.code_review.Workspace"),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            committed, _ = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
            )

        assert committed is False
        git_mock.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_container_error_does_not_propagate(self):
        """Container failure returns False and does not raise."""
        from forge.workflow.nodes.code_review import run_post_change_review

        runner_mock = MagicMock()
        runner_mock.run = AsyncMock(side_effect=RuntimeError("container crashed"))

        with (
            patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            committed, result = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
            )

        assert committed is False
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_container_result_for_exhaustion_propagation(self):
        """run_post_change_review must return ContainerResult so callers can merge exhaustion.

        P2.2: when the post-change review skill exhausts retries, the exhaustion
        data was silently dropped because the utility returned only a bool. Callers
        (ci_evaluator, implement_review) need the ContainerResult to call
        merge_review_exhaustion on it.
        """
        from forge.workflow.nodes.code_review import run_post_change_review

        exhausted_result = ContainerResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
            review_cycles=[
                ReviewCycleData(
                    cycle=1,
                    max_cycles=1,
                    verdict="rejected",
                    feedback="auto-review exhausted",
                    skill="review-code",
                    elapsed_seconds=5.0,
                    timestamp="",
                )
            ],
        )

        git_mock = MagicMock()
        git_mock.has_uncommitted_changes.return_value = False
        runner_mock = MagicMock()
        runner_mock.run = AsyncMock(return_value=exhausted_result)

        with (
            patch("forge.workflow.nodes.code_review.ContainerRunner", return_value=runner_mock),
            patch("forge.workflow.nodes.code_review.GitOperations", return_value=git_mock),
            patch("forge.workflow.nodes.code_review.Workspace"),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            committed, container_result = await run_post_change_review(
                workspace_path="/tmp/ws",
                ticket_key="TEST-123",
                current_repo="org/repo",
                branch_name="forge/test-123",
            )

        assert committed is False
        assert container_result is not None, (
            "ContainerResult must be returned for exhaustion propagation"
        )
        assert container_result.review_exhausted is True


# ── sync_pr_description ───────────────────────────────────────────────────────


def _git_mock(commit_log: str = FIX_COMMITS) -> MagicMock:
    git = MagicMock()
    git._run_git.return_value.stdout = commit_log
    return git


def _adapter_jira_mocks(pr_body: str):
    from forge.integrations.source_control.contracts import (
        ChangeRequest,
        ChangeRequestIdentity,
        ChangeRequestState,
    )

    adapter = AsyncMock()
    adapter.get_change_request.return_value = ChangeRequest(
        identity=ChangeRequestIdentity("c", "org/repo", 42),
        url="u",
        title="t",
        body=pr_body,
        state=ChangeRequestState.OPEN,
        source_branch="f",
        target_branch="main",
    )

    jira = MagicMock()
    jira.add_comment = AsyncMock()
    jira.close = AsyncMock()

    return adapter, jira


def _repo_ref():
    from forge.integrations.source_control.contracts import Provider, RepositoryRef

    return RepositoryRef(
        id="org/repo",
        provider=Provider.GITHUB,
        connection="c",
        namespace="org/repo",
        default_branch="main",
        change_request_mode="fork",
    )


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
        adapter, jira = _adapter_jira_mocks(original)

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value=updated)
        agent_mock.close = AsyncMock()
        agent_mock._strip_preamble = MagicMock(side_effect=lambda x: x)

        with (
            patch(
                "forge.workflow.nodes.code_review.get_adapter", return_value=(_repo_ref(), adapter)
            ),
            patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira),
            patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            await sync_pr_description(
                state,
                _git_mock(),
                current_repo="org/repo",
                pr_number=42,
                attempt=2,
            )

        adapter.update_change_request.assert_called_once()
        _, kwargs = adapter.update_change_request.call_args
        assert kwargs["body"] == updated
        jira.add_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_body_unchanged(self, state):
        """No PR update or Jira comment when the body is already accurate."""
        from forge.workflow.nodes.code_review import sync_pr_description

        body = "The jitter is +-10% uniform."
        adapter, jira = _adapter_jira_mocks(body)

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value=body)
        agent_mock.close = AsyncMock()
        agent_mock._strip_preamble = MagicMock(side_effect=lambda x: x)

        with (
            patch(
                "forge.workflow.nodes.code_review.get_adapter", return_value=(_repo_ref(), adapter)
            ),
            patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira),
            patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            await sync_pr_description(
                state,
                _git_mock(),
                current_repo="org/repo",
                pr_number=42,
                attempt=2,
            )

        adapter.update_change_request.assert_not_called()
        jira.add_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_commits(self, state):
        """Empty commit log skips the agent call entirely."""
        from forge.workflow.nodes.code_review import sync_pr_description

        adapter, jira = _adapter_jira_mocks("body")

        with (
            patch(
                "forge.workflow.nodes.code_review.get_adapter", return_value=(_repo_ref(), adapter)
            ),
            patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira),
            patch("forge.workflow.nodes.code_review.ForgeAgent") as MockAgent,
        ):
            await sync_pr_description(
                state,
                _git_mock(""),
                current_repo="org/repo",
                pr_number=42,
                attempt=1,
            )

        MockAgent.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_pr_number(self, state):
        """No PR number means nothing to update."""
        from forge.workflow.nodes.code_review import sync_pr_description

        with patch("forge.workflow.nodes.code_review.get_adapter") as get_adapter_mock:
            await sync_pr_description(
                state,
                MagicMock(),
                current_repo="org/repo",
                pr_number=None,
                attempt=1,
            )

        get_adapter_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_does_not_propagate(self, state):
        """Agent failure never blocks the caller."""
        from forge.workflow.nodes.code_review import sync_pr_description

        adapter, jira = _adapter_jira_mocks("body")

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(side_effect=RuntimeError("timeout"))
        agent_mock.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.code_review.get_adapter", return_value=(_repo_ref(), adapter)
            ),
            patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira),
            patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            await sync_pr_description(
                state,
                _git_mock(),
                current_repo="org/repo",
                pr_number=42,
                attempt=1,
            )

        adapter.update_change_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_comment_labels_initial_create(self, state):
        """attempt=0 produces a human-readable 'PR creation' label in the comment."""
        from forge.workflow.nodes.code_review import sync_pr_description

        adapter, jira = _adapter_jira_mocks("old body")

        agent_mock = MagicMock()
        agent_mock.run_task = AsyncMock(return_value="new body")
        agent_mock.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.code_review.get_adapter", return_value=(_repo_ref(), adapter)
            ),
            patch("forge.workflow.nodes.code_review.JiraClient", return_value=jira),
            patch("forge.workflow.nodes.code_review.ForgeAgent", return_value=agent_mock),
            patch("forge.workflow.nodes.code_review.load_prompt", return_value="prompt"),
        ):
            await sync_pr_description(
                state,
                _git_mock(),
                current_repo="org/repo",
                pr_number=42,
                attempt=0,
            )

        comment_text = jira.add_comment.call_args[0][1]
        assert "PR creation" in comment_text


# ── integration: sync wired into create_pull_request ─────────────────────────


class TestSyncCalledFromCreatePR:
    """sync_pr_description is called by create_pull_request after PR creation."""

    @pytest.mark.asyncio
    async def test_sync_called_after_pr_creation(self):
        from forge.integrations.source_control.contracts import (
            ChangeRequest,
            ChangeRequestIdentity,
            ChangeRequestState,
            Provider,
            RepositoryRef,
            WriteTarget,
        )
        from forge.workflow.nodes.pr_creation import create_pull_request

        state = make_workflow_state(
            current_node="create_pr",
            current_repo="org/repo",
            implemented_tasks=["TEST-200"],
            workspace_path="/tmp/forge-workspace-test",
            context={"branch_name": "forge/test-123"},
        )

        repo_ref = RepositoryRef(
            id="org/repo",
            provider=Provider.GITHUB,
            connection="c",
            namespace="org/repo",
            default_branch="main",
            change_request_mode="fork",
        )
        mock_adapter = AsyncMock()
        mock_adapter.ensure_write_target = AsyncMock(
            return_value=WriteTarget(
                clone_url="",
                push_remote_name="origin",
                head_ref="",
                base_branch="main",
                fork_owner="fork-user",
                fork_repo="repo",
            )
        )
        mock_adapter.create_change_request = AsyncMock(
            return_value=ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection="c", repository_id="org/repo", native_id=42
                ),
                url="https://github.com/org/repo/pull/42",
                title="t",
                body="b",
                state=ChangeRequestState.OPEN,
                source_branch="f",
                target_branch="main",
                created=True,
            )
        )
        mock_adapter.get_change_request = AsyncMock(
            return_value=ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection="c", repository_id="org/repo", native_id=42
                ),
                url="https://github.com/org/repo/pull/42",
                title="t",
                body="",
                state=ChangeRequestState.OPEN,
                source_branch="f",
                target_branch="main",
            )
        )
        mock_adapter.update_change_request = AsyncMock()
        mock_adapter.create_comment = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.get_issue = AsyncMock(return_value=MagicMock(summary="Test feature"))
        mock_jira.add_comment = AsyncMock()
        mock_jira.create_remote_link = AsyncMock()
        mock_jira.is_repo_draft = AsyncMock(return_value=False)
        mock_jira.close = AsyncMock()

        mock_git = MagicMock()
        mock_git.push_to_fork = MagicMock()
        mock_git.add_fork_remote = MagicMock()

        with (
            patch(
                "forge.workflow.nodes.pr_creation.get_adapter",
                return_value=(repo_ref, mock_adapter),
            ),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch("forge.workflow.nodes.pr_creation.Workspace"),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts",
                AsyncMock(return_value=(False, [])),
            ),
            patch(
                "forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
                AsyncMock(return_value="## Summary\n\nTest PR."),
            ),
            patch("forge.workflow.nodes.pr_creation.set_pr_ticket_index", new_callable=AsyncMock),
            patch(
                "forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock
            ) as mock_sync,
        ):
            await create_pull_request(state)

        mock_sync.assert_called_once()
        _, call_kwargs = mock_sync.call_args
        assert call_kwargs.get("attempt") == 0
