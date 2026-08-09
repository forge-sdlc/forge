"""Tests for spec PR creation and update helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state
from forge.integrations.github.client import PullRequestCreationResult


class TestCreateSpecProposalPr:
    @pytest.mark.asyncio
    async def test_creates_branch_and_pr(self):
        from forge.workflow.nodes.spec_generation import _create_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_or_create_fork = AsyncMock(
            return_value={
                "owner": {"login": "forge-bot"},
                "name": "proposals",
                "default_branch": "trunk",
            }
        )
        mock_gh.get_repository = AsyncMock(return_value={"default_branch": "trunk"})
        mock_gh.sync_fork_with_upstream = AsyncMock(return_value=True)
        mock_gh.get_file_contents = AsyncMock(return_value={"sha": "existing-sha"})
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/spec/test-123"})
        mock_gh.create_or_update_file = AsyncMock(return_value={"content": {"sha": "filesha"}})
        mock_gh.create_pull_request = AsyncMock(
            return_value=PullRequestCreationResult(
                pr={
                    "number": 12,
                    "html_url": "https://github.com/org/proposals/pull/12",
                },
                created=True,
            )
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.proposal_pr.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.proposal_pr.set_pr_ticket_index",
                new_callable=AsyncMock,
            ) as mock_index,
        ):
            result = await _create_spec_proposal_pr(
                ticket_key="TEST-123",
                spec_content="# My Spec",
                summary="My Feature",
                proposals_repo="org/proposals",
            )

        assert result["spec_pr_number"] == 12
        assert result["spec_pr_url"] == "https://github.com/org/proposals/pull/12"
        assert result["spec_pr_repo"] == "org/proposals"
        assert result["spec_pr_branch"] == "forge/spec/test-123"
        assert result["spec_pr_file_path"] == "TEST-123/design.md"

        mock_gh.create_branch.assert_called_once_with(
            "forge-bot", "proposals", "forge/spec/test-123", base="trunk"
        )
        mock_gh.sync_fork_with_upstream.assert_awaited_once_with(
            "forge-bot", "proposals", branch="trunk"
        )
        assert result["spec_pr_fork_owner"] == "forge-bot"
        assert result["spec_pr_fork_repo"] == "proposals"
        assert mock_gh.create_or_update_file.call_args.kwargs["sha"] == "existing-sha"
        mock_gh.create_pull_request.assert_called_once()
        pr_call_kwargs = mock_gh.create_pull_request.call_args[1]
        assert pr_call_kwargs["owner"] == "org"
        assert pr_call_kwargs["repo"] == "proposals"
        assert pr_call_kwargs["head"] == "forge-bot:forge/spec/test-123"
        assert pr_call_kwargs["base"] == "trunk"
        assert "# My Spec" not in pr_call_kwargs["body"]
        assert "TEST-123/design.md" in pr_call_kwargs["body"]
        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
        mock_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_pr_with_custom_path(self):
        from forge.workflow.nodes.spec_generation import _create_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_or_create_fork = AsyncMock(
            return_value={"owner": {"login": "forge-bot"}, "name": "proposals"}
        )
        # Omitting a custom default branch exercises the upstream "main" fallback.
        mock_gh.get_repository = AsyncMock(return_value={})
        mock_gh.sync_fork_with_upstream = AsyncMock(return_value=True)
        mock_gh.get_file_contents = AsyncMock(return_value=None)
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/spec/test-456"})
        mock_gh.create_or_update_file = AsyncMock(return_value={"content": {"sha": "filesha"}})
        mock_gh.create_pull_request = AsyncMock(
            return_value=PullRequestCreationResult(
                pr={
                    "number": 15,
                    "html_url": "https://github.com/org/proposals/pull/15",
                },
                created=True,
            )
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.proposal_pr.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.proposal_pr.set_pr_ticket_index",
                new_callable=AsyncMock,
            ),
        ):
            result = await _create_spec_proposal_pr(
                ticket_key="TEST-456",
                spec_content="# My Spec",
                summary="My Feature",
                proposals_repo="org/proposals",
                proposals_path="/enhancements/",
            )

        assert result["spec_pr_file_path"] == "enhancements/TEST-456/design.md"
        pr_call_kwargs = mock_gh.create_pull_request.call_args[1]
        assert "enhancements/TEST-456/design.md" in pr_call_kwargs["body"]
        assert pr_call_kwargs["base"] == "main"
        assert mock_gh.create_or_update_file.call_args.kwargs["sha"] is None

    @pytest.mark.asyncio
    async def test_stops_when_fork_cannot_be_synchronized(self):
        from forge.workflow.nodes.spec_generation import _create_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_or_create_fork = AsyncMock(
            return_value={"owner": {"login": "forge-bot"}, "name": "proposals"}
        )
        mock_gh.get_repository = AsyncMock(return_value={"default_branch": "main"})
        mock_gh.sync_fork_with_upstream = AsyncMock(return_value=False)
        mock_gh.close = AsyncMock()
        mock_jira = MagicMock(close=AsyncMock())

        with (
            patch("forge.workflow.nodes.proposal_pr.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            pytest.raises(RuntimeError, match="Could not synchronize proposal fork"),
        ):
            await _create_spec_proposal_pr(
                ticket_key="TEST-FAIL",
                spec_content="# Spec",
                summary="Feature",
                proposals_repo="org/proposals",
            )

        mock_gh.create_branch.assert_not_called()
        mock_gh.create_or_update_file.assert_not_called()
        mock_gh.create_pull_request.assert_not_called()


class TestUpdateSpecProposalPr:
    @pytest.mark.asyncio
    async def test_updates_file_on_branch(self):
        from forge.workflow.nodes.spec_generation import _update_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_file_contents = AsyncMock(
            return_value={"sha": "oldsha", "path": "TEST-123/design.md"}
        )
        mock_gh.create_or_update_file = AsyncMock(return_value={"content": {"sha": "newsha"}})
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            spec_pr_branch="forge/spec/test-123",
            spec_pr_repo="org/proposals",
            spec_pr_fork_owner="forge-bot",
            spec_pr_fork_repo="proposals",
            spec_pr_number=12,
            spec_pr_url="https://github.com/org/proposals/pull/12",
            spec_pr_file_path="TEST-123/design.md",
        )

        with patch("forge.workflow.nodes.proposal_pr.GitHubClient", return_value=mock_gh):
            await _update_spec_proposal_pr(
                ticket_key="TEST-123",
                spec_content="# Revised Spec",
                state=state,
            )

        mock_gh.get_file_contents.assert_called_once_with(
            "forge-bot", "proposals", "TEST-123/design.md", "forge/spec/test-123"
        )
        mock_gh.create_or_update_file.assert_called_once()
        call_kwargs = mock_gh.create_or_update_file.call_args[1]
        assert call_kwargs["sha"] == "oldsha"
        assert call_kwargs["path"] == "TEST-123/design.md"
        mock_gh.create_issue_comment.assert_called_once_with(
            "org",
            "proposals",
            12,
            "Specification has been revised based on feedback. Please review the updated version.",
        )

    @pytest.mark.asyncio
    async def test_updates_legacy_upstream_branch_without_fork_state(self):
        from forge.workflow.nodes.spec_generation import _update_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_file_contents = AsyncMock(return_value={"sha": "oldsha"})
        mock_gh.create_or_update_file = AsyncMock()
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()
        state = create_initial_feature_state(
            ticket_key="TEST-LEGACY",
            ticket_type=TicketType.FEATURE,
            spec_pr_repo="org/proposals",
            spec_pr_branch="forge/spec/test-legacy",
            spec_pr_number=13,
            spec_pr_file_path="TEST-LEGACY/design.md",
        )

        with patch("forge.workflow.nodes.proposal_pr.GitHubClient", return_value=mock_gh):
            await _update_spec_proposal_pr("TEST-LEGACY", "# Revised", state)

        mock_gh.get_file_contents.assert_awaited_once_with(
            "org", "proposals", "TEST-LEGACY/design.md", "forge/spec/test-legacy"
        )
        assert mock_gh.create_or_update_file.call_args.kwargs["owner"] == "org"
        assert mock_gh.create_or_update_file.call_args.kwargs["repo"] == "proposals"
