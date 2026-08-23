"""Unit tests for draft PR creation behavior and repository metadata configuration."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient, MissingProjectConfig
from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    Provider,
    RepositoryRef,
    WriteTarget,
)
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.nodes.pr_creation import create_pull_request


def _repo_ref(identifier: str) -> RepositoryRef:
    return RepositoryRef(
        id=identifier,
        provider=Provider.GITHUB,
        connection="c",
        namespace=identifier,
        default_branch="main",
        change_request_mode="fork",
    )


def create_mock_adapter(pr_number=123, pr_url="https://github.com/owner/repo/pull/123"):
    """Create a mock SourceControlProvider adapter with configurable PR data."""
    adapter = AsyncMock()
    adapter.ensure_write_target = AsyncMock(
        return_value=WriteTarget(
            clone_url="",
            push_remote_name="origin",
            head_ref="",
            base_branch="main",
            fork_owner="fork-owner",
            fork_repo="repo",
        )
    )
    adapter.create_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="c", repository_id="owner/repo", native_id=pr_number
            ),
            url=pr_url,
            title="t",
            body="b",
            state=ChangeRequestState.OPEN,
            source_branch="f",
            target_branch="main",
            created=True,
        )
    )
    adapter.get_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="c", repository_id="owner/repo", native_id=pr_number
            ),
            url=pr_url,
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="f",
            target_branch="main",
        )
    )
    adapter.update_change_request = AsyncMock()
    adapter.create_comment = AsyncMock()
    return adapter


def create_mock_jira_client():
    """Create a mock JiraClient."""
    mock = MagicMock()
    mock.close = AsyncMock()
    mock.add_comment = AsyncMock()
    mock.create_remote_link = AsyncMock()
    mock.get_issue = AsyncMock()
    mock.set_workflow_label = AsyncMock()
    mock.is_repo_draft = AsyncMock(return_value=False)

    # Mock issue with summary
    mock_issue = MagicMock()
    mock_issue.summary = "Test feature"
    mock.get_issue.return_value = mock_issue

    return mock


def create_mock_git_operations():
    """Create a mock GitOperations."""
    mock = MagicMock()
    mock.add_fork_remote = MagicMock()
    mock.push_to_fork = MagicMock()

    mock_result = MagicMock()
    mock_result.stdout = "abc123 Test commit\n\nTest commit body"
    mock._run_git = MagicMock(return_value=mock_result)

    return mock


def create_mock_workspace():
    """Create a mock Workspace."""
    mock = MagicMock()
    mock.path = Path("/tmp/test-workspace")
    return mock


class TestJiraConfigParser:
    """Tests JiraClient configuration parsing for repos metadata and draft PR settings."""

    @pytest.fixture
    def mock_jira_client(self):
        """Create a JiraClient with mocked settings."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"
            return JiraClient()

    @pytest.mark.asyncio
    async def test_get_project_repos_mixed_list(self, mock_jira_client):
        """Successfully parses mixed list of strings and metadata dicts in forge.repos."""
        mock_jira_client.get_project_property = AsyncMock(
            return_value=[
                "owner/repo1",
                {"name": "owner/repo2", "draft": True},
                {"name": "owner/repo3", "draft_pr": True},
                {"name": "owner/repo4"},
            ]
        )

        repos = await mock_jira_client.get_project_repos("PROJ")
        assert repos == ["owner/repo1", "owner/repo2", "owner/repo3", "owner/repo4"]

    @pytest.mark.asyncio
    async def test_get_project_repos_malformed_raises_error(self, mock_jira_client):
        """Raises MissingProjectConfig if forge.repos is not a list or has malformed items."""
        # Malformed: not a list
        mock_jira_client.get_project_property = AsyncMock(return_value="not-a-list")
        with pytest.raises(MissingProjectConfig):
            await mock_jira_client.get_project_repos("PROJ")

        # Malformed: string without '/'
        mock_jira_client.get_project_property = AsyncMock(return_value=["malformed-name"])
        with pytest.raises(MissingProjectConfig):
            await mock_jira_client.get_project_repos("PROJ")

        # Malformed: dict without name
        mock_jira_client.get_project_property = AsyncMock(return_value=[{"draft": True}])
        with pytest.raises(MissingProjectConfig):
            await mock_jira_client.get_project_repos("PROJ")

    @pytest.mark.asyncio
    async def test_is_repo_draft(self, mock_jira_client):
        """Correctly resolves draft setting for various repo configurations."""
        mock_jira_client.get_project_property = AsyncMock(
            return_value=[
                "owner/repo1",
                {"name": "owner/repo2", "draft": True},
                {"name": "owner/repo3", "draft_pr": True},
                {"name": "owner/repo4", "draft": False},
                {"name": "owner/repo5"},
                {"name": "owner/repo6", "draft": "false"},
                {"name": "owner/repo7", "draft_pr": 1},
            ]
        )

        # String entry: should be False
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo1") is False

        # Dict with draft=True: should be True
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo2") is True

        # Dict with draft_pr=True: should be True
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo3") is True

        # Case-insensitive resolution: should be True
        assert await mock_jira_client.is_repo_draft("PROJ", "OwNeR/RePo2") is True

        # Dict with draft=False: should be False
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo4") is False

        # Dict without draft keys: should be False
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo5") is False

        # Non-boolean values should not enable draft PRs
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo6") is False
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo7") is False

        # Non-existent repository: should be False
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/nonexistent") is False

    @pytest.mark.asyncio
    async def test_is_repo_draft_on_missing_property(self, mock_jira_client):
        """is_repo_draft should gracefully fall back to False if project property is missing or errors."""
        mock_jira_client.get_project_property = AsyncMock(side_effect=Exception("API Error"))
        assert await mock_jira_client.is_repo_draft("PROJ", "owner/repo1") is False


class TestGitHubClientDraft:
    """Tests GitHubClient PR creation request payloads with draft options."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a GitHubClient with mocked settings."""
        with patch("forge.integrations.github.client.get_settings") as mock_settings:
            mock_settings.return_value.github_token = MagicMock()
            mock_settings.return_value.github_token.get_secret_value.return_value = "token"
            mock_settings.return_value.github_fork_owner = ""
            return GitHubClient()

    @pytest.mark.asyncio
    async def test_create_pull_request_with_draft_true(self, mock_github_client):
        """create_pull_request passes draft=True in payload when draft is True."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "number": 123,
            "html_url": "https://github.com/owner/repo/pull/123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_github_client, "_get_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http_client

            await mock_github_client.create_pull_request(
                owner="owner",
                repo="repo",
                title="test pr",
                body="body",
                head="branch",
                base="main",
                draft=True,
            )

            mock_http_client.post.assert_called_once()
            call_json = mock_http_client.post.call_args.kwargs["json"]
            assert call_json["draft"] is True

    @pytest.mark.asyncio
    async def test_create_pull_request_with_draft_false(self, mock_github_client):
        """create_pull_request does not include draft in payload when draft is False."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "number": 123,
            "html_url": "https://github.com/owner/repo/pull/123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_github_client, "_get_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http_client

            await mock_github_client.create_pull_request(
                owner="owner",
                repo="repo",
                title="test pr",
                body="body",
                head="branch",
                base="main",
                draft=False,
            )

            mock_http_client.post.assert_called_once()
            call_json = mock_http_client.post.call_args.kwargs["json"]
            assert "draft" not in call_json


class TestPRCreationNodeDraft:
    """Tests the PR creation node workflow when draft PR option is enabled vs disabled."""

    @pytest.mark.asyncio
    async def test_create_pr_node_invokes_github_with_draft_true(self):
        """Workflow node should invoke the adapter with draft=True if repository configured as draft."""
        mock_adapter = create_mock_adapter(pr_number=101)
        mock_jira = create_mock_jira_client()
        mock_jira.is_repo_draft = AsyncMock(return_value=True)
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

        with (
            patch(
                "forge.workflow.nodes.pr_creation.get_adapter",
                return_value=(_repo_ref("owner/repo"), mock_adapter),
            ),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch(
                "forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
                new_callable=AsyncMock,
                return_value="Generated PR body",
            ),
            patch("forge.workflow.nodes.pr_creation.set_pr_ticket_index", new_callable=AsyncMock),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify is_repo_draft was resolved
        mock_jira.is_repo_draft.assert_called_once_with("FEAT", "owner/repo")

        # Verify create_change_request was called with draft=True and the fork head
        mock_adapter.create_change_request.assert_called_once()
        _, kwargs = mock_adapter.create_change_request.call_args
        assert kwargs["title"] == "[FEAT-123] Test feature"
        assert kwargs["target"].head_ref == "feat/test"
        assert kwargs["target"].base_branch == "main"
        assert kwargs["target"].fork_owner == "fork-owner"
        assert kwargs["draft"] is True

    @pytest.mark.asyncio
    async def test_create_pr_node_invokes_github_with_draft_false(self):
        """Workflow node should invoke the adapter with draft=False if repository is not configured as draft."""
        mock_adapter = create_mock_adapter(pr_number=102)
        mock_jira = create_mock_jira_client()
        mock_jira.is_repo_draft = AsyncMock(return_value=False)
        mock_git = create_mock_git_operations()

        state = create_initial_feature_state(
            ticket_key="FEAT-123",
            current_repo="owner/repo",
        )
        state["workspace_path"] = "/tmp/test-workspace"
        state["implemented_tasks"] = ["TASK-1"]
        state["context"] = {"branch_name": "feat/test"}

        with (
            patch(
                "forge.workflow.nodes.pr_creation.get_adapter",
                return_value=(_repo_ref("owner/repo"), mock_adapter),
            ),
            patch("forge.workflow.nodes.pr_creation.JiraClient", return_value=mock_jira),
            patch("forge.workflow.nodes.pr_creation.GitOperations", return_value=mock_git),
            patch(
                "forge.workflow.nodes.pr_creation.Workspace", return_value=create_mock_workspace()
            ),
            patch(
                "forge.workflow.nodes.pr_creation.check_merge_conflicts", return_value=(False, [])
            ),
            patch(
                "forge.workflow.nodes.pr_creation._generate_pr_body_with_agent",
                new_callable=AsyncMock,
                return_value="Generated PR body",
            ),
            patch("forge.workflow.nodes.pr_creation.set_pr_ticket_index", new_callable=AsyncMock),
            patch("forge.workflow.nodes.pr_creation.sync_pr_description", new_callable=AsyncMock),
        ):
            await create_pull_request(state)

        # Verify is_repo_draft was resolved
        mock_jira.is_repo_draft.assert_called_once_with("FEAT", "owner/repo")

        # Verify create_change_request was called with draft=False
        mock_adapter.create_change_request.assert_called_once()
        _, kwargs = mock_adapter.create_change_request.call_args
        assert kwargs["title"] == "[FEAT-123] Test feature"
        assert kwargs["target"].head_ref == "feat/test"
        assert kwargs["target"].base_branch == "main"
        assert kwargs["draft"] is False
