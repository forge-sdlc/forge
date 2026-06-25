"""Tests for spec PR creation and update helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state


class TestCreateSpecProposalPr:
    @pytest.mark.asyncio
    async def test_creates_branch_and_pr(self):
        from forge.workflow.nodes.spec_generation import _create_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/spec/test-123"})
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "filesha"}}
        )
        mock_gh.create_pull_request = AsyncMock(
            return_value={
                "number": 12,
                "html_url": "https://github.com/org/proposals/pull/12",
            }
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.spec_generation.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.spec_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.spec_generation.set_pr_ticket_index",
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

        mock_gh.create_branch.assert_called_once_with("org", "proposals", "forge/spec/test-123")
        mock_gh.create_pull_request.assert_called_once()
        pr_call_kwargs = mock_gh.create_pull_request.call_args[1]
        assert "# My Spec" not in pr_call_kwargs["body"]
        assert "TEST-123/design.md" in pr_call_kwargs["body"]
        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
        mock_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_pr_with_custom_path(self):
        from forge.workflow.nodes.spec_generation import _create_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/spec/test-456"})
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "filesha"}}
        )
        mock_gh.create_pull_request = AsyncMock(
            return_value={
                "number": 15,
                "html_url": "https://github.com/org/proposals/pull/15",
            }
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.spec_generation.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.spec_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.spec_generation.set_pr_ticket_index",
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


class TestUpdateSpecProposalPr:
    @pytest.mark.asyncio
    async def test_updates_file_on_branch(self):
        from forge.workflow.nodes.spec_generation import _update_spec_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_file_contents = AsyncMock(
            return_value={"sha": "oldsha", "path": "TEST-123/design.md"}
        )
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "newsha"}}
        )
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            spec_pr_branch="forge/spec/test-123",
            spec_pr_repo="org/proposals",
            spec_pr_number=12,
            spec_pr_url="https://github.com/org/proposals/pull/12",
            spec_pr_file_path="TEST-123/design.md",
        )

        with patch("forge.workflow.nodes.spec_generation.GitHubClient", return_value=mock_gh):
            await _update_spec_proposal_pr(
                ticket_key="TEST-123",
                spec_content="# Revised Spec",
                state=state,
            )

        mock_gh.get_file_contents.assert_called_once_with(
            "org", "proposals", "TEST-123/design.md", "forge/spec/test-123"
        )
        mock_gh.create_or_update_file.assert_called_once()
        call_kwargs = mock_gh.create_or_update_file.call_args[1]
        assert call_kwargs["sha"] == "oldsha"
        assert call_kwargs["path"] == "TEST-123/design.md"
        mock_gh.create_issue_comment.assert_called_once()
