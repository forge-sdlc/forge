"""Tests for PRD PR creation and update helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state


class TestCreatePrdProposalPr:
    @pytest.mark.asyncio
    async def test_creates_branch_and_pr(self):
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_gh = MagicMock()
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/prd/test-123"})
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "filesha"}}
        )
        mock_gh.create_pull_request = AsyncMock(
            return_value={
                "number": 7,
                "html_url": "https://github.com/org/proposals/pull/7",
            }
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.prd_generation.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.prd_generation.set_pr_ticket_index",
                new_callable=AsyncMock,
            ) as mock_index,
        ):
            result = await _create_prd_proposal_pr(
                ticket_key="TEST-123",
                prd_content="# My PRD",
                summary="My Feature",
                proposals_repo="org/proposals",
            )

        assert result["prd_pr_number"] == 7
        assert result["prd_pr_url"] == "https://github.com/org/proposals/pull/7"
        assert result["prd_pr_repo"] == "org/proposals"
        assert result["prd_pr_branch"] == "forge/prd/test-123"
        assert result["prd_pr_file_path"] == "TEST-123/prd.md"

        mock_gh.create_branch.assert_called_once_with("org", "proposals", "forge/prd/test-123")
        mock_gh.create_pull_request.assert_called_once()
        pr_call_kwargs = mock_gh.create_pull_request.call_args[1]
        assert "# My PRD" not in pr_call_kwargs["body"]
        assert "TEST-123/prd.md" in pr_call_kwargs["body"]
        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
        mock_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_pr_with_custom_path(self):
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_gh = MagicMock()
        mock_gh.create_branch = AsyncMock(return_value={"ref": "refs/heads/forge/prd/test-456"})
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "filesha"}}
        )
        mock_gh.create_pull_request = AsyncMock(
            return_value={
                "number": 10,
                "html_url": "https://github.com/org/proposals/pull/10",
            }
        )
        mock_gh.close = AsyncMock()

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch("forge.workflow.nodes.prd_generation.GitHubClient", return_value=mock_gh),
            patch("forge.workflow.nodes.prd_generation.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.prd_generation.set_pr_ticket_index",
                new_callable=AsyncMock,
            ),
        ):
            result = await _create_prd_proposal_pr(
                ticket_key="TEST-456",
                prd_content="# My PRD",
                summary="My Feature",
                proposals_repo="org/proposals",
                proposals_path="enhancements",
            )

        assert result["prd_pr_file_path"] == "enhancements/TEST-456/prd.md"
        pr_call_kwargs = mock_gh.create_pull_request.call_args[1]
        assert "enhancements/TEST-456/prd.md" in pr_call_kwargs["body"]


class TestResolveProposalsPath:
    @pytest.mark.asyncio
    async def test_normalizes_global_fallback_path(self):
        from forge.workflow.nodes.prd_generation import _resolve_proposals_path

        mock_jira = MagicMock()
        mock_jira.get_proposals_path = AsyncMock(return_value=None)
        mock_settings = MagicMock()
        mock_settings.prd_proposals_path = "/enhancements/"

        with patch("forge.workflow.nodes.prd_generation.get_settings", return_value=mock_settings):
            result = await _resolve_proposals_path("TEST", mock_jira)

        assert result == "enhancements"


class TestUpdatePrdProposalPr:
    @pytest.mark.asyncio
    async def test_updates_file_on_branch(self):
        from forge.workflow.nodes.prd_generation import _update_prd_proposal_pr

        mock_gh = MagicMock()
        mock_gh.get_file_contents = AsyncMock(
            return_value={"sha": "oldsha", "path": "TEST-123/prd.md"}
        )
        mock_gh.create_or_update_file = AsyncMock(
            return_value={"content": {"sha": "newsha"}}
        )
        mock_gh.create_issue_comment = AsyncMock()
        mock_gh.close = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            prd_pr_branch="forge/prd/test-123",
            prd_pr_repo="org/proposals",
            prd_pr_number=7,
            prd_pr_url="https://github.com/org/proposals/pull/7",
            prd_pr_file_path="TEST-123/prd.md",
        )

        with patch("forge.workflow.nodes.prd_generation.GitHubClient", return_value=mock_gh):
            await _update_prd_proposal_pr(
                ticket_key="TEST-123",
                prd_content="# Revised PRD",
                state=state,
            )

        mock_gh.get_file_contents.assert_called_once_with(
            "org", "proposals", "TEST-123/prd.md", "forge/prd/test-123"
        )
        mock_gh.create_or_update_file.assert_called_once()
        call_kwargs = mock_gh.create_or_update_file.call_args[1]
        assert call_kwargs["sha"] == "oldsha"
        assert call_kwargs["path"] == "TEST-123/prd.md"
        mock_gh.create_issue_comment.assert_called_once()
