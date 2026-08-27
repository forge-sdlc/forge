"""Tests for PRD PR creation and update helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    Provider,
    RepositoryRef,
    WriteTarget,
)
from forge.integrations.source_control.errors import ConflictError
from forge.models.workflow import TicketType
from forge.workflow.feature.state import create_initial_feature_state


def _repo_ref(identifier: str = "org/proposals") -> RepositoryRef:
    return RepositoryRef(
        id=identifier,
        provider=Provider.GITHUB,
        connection="c",
        namespace=identifier,
        default_branch="main",
        change_request_mode="fork",
    )


def _fork_ref(fork_owner: str, fork_repo: str) -> RepositoryRef:
    return RepositoryRef(
        id=f"{fork_owner}/{fork_repo}",
        provider=Provider.GITHUB,
        connection="c",
        namespace=f"{fork_owner}/{fork_repo}",
        default_branch="main",
        change_request_mode="direct",
    )


class TestCreatePrdProposalPr:
    @pytest.mark.asyncio
    async def test_creates_branch_and_pr(self):
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_adapter = AsyncMock()
        mock_adapter.resolve_default_branch = AsyncMock(return_value="trunk")
        mock_adapter.ensure_write_target = AsyncMock(
            return_value=WriteTarget(
                clone_url="",
                push_remote_name="origin",
                head_ref="",
                base_branch="main",
                fork_owner="forge-bot",
                fork_repo="proposals",
            )
        )
        mock_adapter.create_branch = AsyncMock()
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_change_request = AsyncMock(
            return_value=ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection="c", repository_id="org/proposals", native_id=7
                ),
                url="https://github.com/org/proposals/pull/7",
                title="t",
                body="b",
                state=ChangeRequestState.OPEN,
                source_branch="forge/prd/test-123",
                target_branch="trunk",
            )
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.proposal_pr.get_adapter",
                return_value=(_repo_ref(), mock_adapter),
            ),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.proposal_pr.set_pr_ticket_index",
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

        mock_adapter.create_branch.assert_called_once_with(
            _fork_ref("forge-bot", "proposals"), "forge/prd/test-123", "trunk"
        )
        mock_adapter.put_file.assert_called_once()
        put_args = mock_adapter.put_file.call_args[0]
        assert put_args[0] == _fork_ref("forge-bot", "proposals")
        assert put_args[1] == "TEST-123/prd.md"
        assert put_args[2] == "# My PRD"
        assert put_args[4] == "forge/prd/test-123"

        mock_adapter.create_change_request.assert_called_once()
        cr_args, cr_kwargs = mock_adapter.create_change_request.call_args
        assert cr_args[0] == _repo_ref()
        assert cr_args[1].head_ref == "forge/prd/test-123"
        assert cr_args[1].base_branch == "trunk"
        assert cr_args[1].fork_owner == "forge-bot"
        assert "# My PRD" not in cr_kwargs["body"]
        assert "TEST-123/prd.md" in cr_kwargs["body"]

        mock_jira.add_comment.assert_called_once()
        mock_jira.set_workflow_label.assert_called_once()
        mock_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_stores_canonical_namespace_not_repos_yaml_alias(self):
        """prd_pr_repo must be the canonical namespace, not the raw (possibly
        repos.yaml-alias) proposals_repo -- webhook matching in worker.py's
        _is_prd_pr_event compares this against event.repo_ref.namespace,
        which is always canonical."""
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_adapter = AsyncMock()
        mock_adapter.resolve_default_branch = AsyncMock(return_value="main")
        mock_adapter.ensure_write_target = AsyncMock(
            return_value=WriteTarget(
                clone_url="",
                push_remote_name="origin",
                head_ref="",
                base_branch="main",
                fork_owner="forge-bot",
                fork_repo="proposals",
            )
        )
        mock_adapter.create_branch = AsyncMock()
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_change_request = AsyncMock(
            return_value=ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection="c", repository_id="org/proposals", native_id=7
                ),
                url="https://github.com/org/proposals/pull/7",
                title="t",
                body="b",
                state=ChangeRequestState.OPEN,
                source_branch="forge/prd/test-123",
                target_branch="main",
            )
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.proposal_pr.get_adapter",
                return_value=(_repo_ref("org/proposals"), mock_adapter),
            ),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.proposal_pr.set_pr_ticket_index",
                new_callable=AsyncMock,
            ),
        ):
            result = await _create_prd_proposal_pr(
                ticket_key="TEST-123",
                prd_content="# My PRD",
                summary="My Feature",
                proposals_repo="proposals-alias",
            )

        assert result["prd_pr_repo"] == "org/proposals"
        assert result["prd_pr_branch"] == "forge/prd/test-123"
        assert result["prd_pr_file_path"] == "TEST-123/prd.md"
        assert result["prd_pr_fork_owner"] == "forge-bot"
        assert result["prd_pr_fork_repo"] == "proposals"

    @pytest.mark.asyncio
    async def test_creates_pr_with_custom_path(self):
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_adapter = AsyncMock()
        mock_adapter.resolve_default_branch = AsyncMock(return_value="main")
        mock_adapter.ensure_write_target = AsyncMock(
            return_value=WriteTarget(
                clone_url="",
                push_remote_name="origin",
                head_ref="",
                base_branch="main",
                fork_owner="forge-bot",
                fork_repo="proposals",
            )
        )
        mock_adapter.create_branch = AsyncMock()
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_change_request = AsyncMock(
            return_value=ChangeRequest(
                identity=ChangeRequestIdentity(
                    connection="c", repository_id="org/proposals", native_id=10
                ),
                url="https://github.com/org/proposals/pull/10",
                title="t",
                body="b",
                state=ChangeRequestState.OPEN,
                source_branch="forge/prd/test-456",
                target_branch="main",
            )
        )

        mock_jira = MagicMock()
        mock_jira.add_comment = AsyncMock()
        mock_jira.set_workflow_label = AsyncMock()
        mock_jira.close = AsyncMock()

        with (
            patch(
                "forge.workflow.nodes.proposal_pr.get_adapter",
                return_value=(_repo_ref(), mock_adapter),
            ),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            patch(
                "forge.workflow.nodes.proposal_pr.set_pr_ticket_index",
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
        cr_args, cr_kwargs = mock_adapter.create_change_request.call_args
        assert "enhancements/TEST-456/prd.md" in cr_kwargs["body"]
        assert cr_args[1].base_branch == "main"

    @pytest.mark.asyncio
    async def test_stops_when_fork_cannot_be_synchronized(self):
        from forge.workflow.nodes.prd_generation import _create_prd_proposal_pr

        mock_adapter = AsyncMock()
        mock_adapter.resolve_default_branch = AsyncMock(return_value="main")
        mock_adapter.ensure_write_target = AsyncMock(
            side_effect=ConflictError("Could not synchronize proposal fork")
        )
        mock_jira = MagicMock(close=AsyncMock())

        with (
            patch(
                "forge.workflow.nodes.proposal_pr.get_adapter",
                return_value=(_repo_ref(), mock_adapter),
            ),
            patch("forge.workflow.nodes.proposal_pr.JiraClient", return_value=mock_jira),
            pytest.raises(ConflictError, match="Could not synchronize proposal fork"),
        ):
            await _create_prd_proposal_pr(
                ticket_key="TEST-FAIL",
                prd_content="# PRD",
                summary="Feature",
                proposals_repo="org/proposals",
            )

        mock_adapter.create_branch.assert_not_called()
        mock_adapter.put_file.assert_not_called()
        mock_adapter.create_change_request.assert_not_called()


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

        mock_adapter = AsyncMock()
        mock_adapter.get_file = AsyncMock(return_value="# Old PRD")
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_comment = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            prd_pr_branch="forge/prd/test-123",
            prd_pr_repo="org/proposals",
            prd_pr_fork_owner="forge-bot",
            prd_pr_fork_repo="proposals",
            prd_pr_number=7,
            prd_pr_url="https://github.com/org/proposals/pull/7",
            prd_pr_file_path="TEST-123/prd.md",
        )

        with patch(
            "forge.workflow.nodes.proposal_pr.get_adapter",
            return_value=(_repo_ref(), mock_adapter),
        ):
            await _update_prd_proposal_pr(
                ticket_key="TEST-123",
                prd_content="# Revised PRD",
                state=state,
            )

        mock_adapter.get_file.assert_called_once_with(
            _fork_ref("forge-bot", "proposals"), "TEST-123/prd.md", "forge/prd/test-123"
        )
        mock_adapter.put_file.assert_called_once()
        put_args = mock_adapter.put_file.call_args[0]
        assert put_args[0] == _fork_ref("forge-bot", "proposals")
        assert put_args[1] == "TEST-123/prd.md"
        assert put_args[2] == "# Revised PRD"
        mock_adapter.create_comment.assert_called_once()
        comment_args = mock_adapter.create_comment.call_args[0]
        assert comment_args[0] == _repo_ref()
        assert comment_args[1].native_id == 7
        assert (
            comment_args[2]
            == "PRD has been revised based on feedback. Please review the updated version."
        )

    @pytest.mark.asyncio
    async def test_updates_legacy_upstream_branch_without_fork_state(self):
        from forge.workflow.nodes.prd_generation import _update_prd_proposal_pr

        mock_adapter = AsyncMock()
        mock_adapter.get_file = AsyncMock(return_value="# Old")
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_comment = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-LEGACY",
            ticket_type=TicketType.FEATURE,
            prd_pr_repo="org/proposals",
            prd_pr_branch="forge/prd/test-legacy",
            prd_pr_number=8,
            prd_pr_file_path="TEST-LEGACY/prd.md",
        )

        with patch(
            "forge.workflow.nodes.proposal_pr.get_adapter",
            return_value=(_repo_ref(), mock_adapter),
        ):
            await _update_prd_proposal_pr("TEST-LEGACY", "# Revised", state)

        mock_adapter.get_file.assert_awaited_once_with(
            _repo_ref(), "TEST-LEGACY/prd.md", "forge/prd/test-legacy"
        )
        put_args = mock_adapter.put_file.call_args[0]
        assert put_args[0] == _repo_ref()

    @pytest.mark.asyncio
    async def test_skips_commit_when_content_unchanged(self):
        from forge.workflow.nodes.prd_generation import _update_prd_proposal_pr

        unchanged_content = "# Same PRD"

        mock_adapter = AsyncMock()
        mock_adapter.get_file = AsyncMock(return_value=unchanged_content)
        mock_adapter.put_file = AsyncMock()
        mock_adapter.create_comment = AsyncMock()

        state = create_initial_feature_state(
            ticket_key="TEST-123",
            ticket_type=TicketType.FEATURE,
            prd_pr_branch="forge/prd/test-123",
            prd_pr_repo="org/proposals",
            prd_pr_fork_owner="forge-bot",
            prd_pr_fork_repo="proposals",
            prd_pr_number=7,
            prd_pr_url="https://github.com/org/proposals/pull/7",
            prd_pr_file_path="TEST-123/prd.md",
        )

        with patch(
            "forge.workflow.nodes.proposal_pr.get_adapter",
            return_value=(_repo_ref(), mock_adapter),
        ):
            await _update_prd_proposal_pr(
                ticket_key="TEST-123",
                prd_content=unchanged_content,
                state=state,
            )

        mock_adapter.put_file.assert_not_called()
        mock_adapter.create_comment.assert_called_once()
        comment_body = mock_adapter.create_comment.call_args[0][2]
        assert "unchanged" in comment_body
