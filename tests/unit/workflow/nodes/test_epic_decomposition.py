"""Unit tests for epic decomposition node — repo resolution paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.client import MissingProjectConfig
from forge.models.workflow import ForgeLabel
from forge.workflow.nodes.epic_decomposition import decompose_epics


@pytest.fixture
def base_state():
    return {
        "ticket_key": "MYPROJ-1",
        "spec_content": "Build a backend service.",
        "qa_history": [],
        "generation_context": {},
        "retry_count": 0,
    }


@pytest.fixture
def mock_issue():
    issue = MagicMock()
    issue.project_key = "MYPROJ"
    issue.summary = "Test Feature"
    return issue


@pytest.fixture
def mock_epics_data():
    return [{"summary": "Epic One", "plan": "Do stuff.", "repo": "acme/backend"}]


class TestDecomposeEpicsRepoResolution:
    """Tests for how decompose_epics resolves available repos."""

    @pytest.mark.asyncio
    async def test_uses_project_repos_from_jira_property(
        self, base_state, mock_issue, mock_epics_data
    ):
        """decompose_epics passes forge.repos project property to the agent context."""
        with (
            patch("forge.workflow.nodes.epic_decomposition.JiraClient") as MockJira,
            patch("forge.workflow.nodes.epic_decomposition.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.epic_decomposition.post_qa_summary_if_needed"),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(return_value=mock_issue)
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.get_project_repos = AsyncMock(return_value=["acme/backend", "acme/frontend"])
            mock_jira.create_epic = AsyncMock(return_value="MYPROJ-100")
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.add_comment = AsyncMock()

            mock_agent = AsyncMock()
            MockAgent.return_value = mock_agent
            captured_context: dict = {}

            async def capture_generate_epics(spec, context):
                captured_context.update(context)
                return mock_epics_data

            mock_agent.generate_epics = capture_generate_epics

            await decompose_epics(base_state)

        assert "acme/backend" in captured_context["available_repos"]
        assert "acme/frontend" in captured_context["available_repos"]

    @pytest.mark.asyncio
    async def test_also_includes_label_repos_alongside_project_repos(
        self, base_state, mock_issue, mock_epics_data
    ):
        """Repos from Feature labels are merged with forge.repos project property."""
        with (
            patch("forge.workflow.nodes.epic_decomposition.JiraClient") as MockJira,
            patch("forge.workflow.nodes.epic_decomposition.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.epic_decomposition.post_qa_summary_if_needed"),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(return_value=mock_issue)
            mock_jira.get_labels = AsyncMock(return_value=["repo:acme/infra"])
            mock_jira.get_project_repos = AsyncMock(return_value=["acme/backend"])
            mock_jira.create_epic = AsyncMock(return_value="MYPROJ-100")
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.add_comment = AsyncMock()

            mock_agent = AsyncMock()
            MockAgent.return_value = mock_agent
            captured_context: dict = {}

            async def capture_generate_epics(spec, context):
                captured_context.update(context)
                return mock_epics_data

            mock_agent.generate_epics = capture_generate_epics

            await decompose_epics(base_state)

        repos = set(captured_context["available_repos"])
        assert "acme/infra" in repos
        assert "acme/backend" in repos

    @pytest.mark.asyncio
    async def test_blocks_and_comments_when_forge_repos_missing(self, base_state, mock_issue):
        """Posts blocking comment and sets forge:blocked when forge.repos is not set."""
        mock_settings = MagicMock()
        mock_settings.forge_require_project_config = True
        mock_settings.known_repos = []
        with (
            patch("forge.workflow.nodes.epic_decomposition.JiraClient") as MockJira,
            patch("forge.workflow.nodes.epic_decomposition.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.epic_decomposition.post_qa_summary_if_needed"),
            patch("forge.workflow.nodes.epic_decomposition.get_settings", return_value=mock_settings),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(return_value=mock_issue)
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.get_project_repos = AsyncMock(
                side_effect=MissingProjectConfig("forge.repos not set for project MYPROJ")
            )
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.add_comment = AsyncMock()

            MockAgent.return_value = AsyncMock()

            result = await decompose_epics(base_state)

        mock_jira.add_comment.assert_called_once()
        comment_text = mock_jira.add_comment.call_args[0][1]
        assert "forge.repos" in comment_text
        assert "forge:retry" in comment_text

        mock_jira.set_workflow_label.assert_called_once_with(
            "MYPROJ-1", ForgeLabel.BLOCKED
        )

        assert result["last_error"]
        assert result["current_node"] == "decompose_epics"

    @pytest.mark.asyncio
    async def test_blocks_and_comments_when_forge_repos_malformed(self, base_state, mock_issue):
        """Posts blocking comment and sets forge:blocked when forge.repos has invalid entries."""
        mock_settings = MagicMock()
        mock_settings.forge_require_project_config = True
        mock_settings.known_repos = []
        with (
            patch("forge.workflow.nodes.epic_decomposition.JiraClient") as MockJira,
            patch("forge.workflow.nodes.epic_decomposition.ForgeAgent") as MockAgent,
            patch("forge.workflow.nodes.epic_decomposition.post_qa_summary_if_needed"),
            patch("forge.workflow.nodes.epic_decomposition.get_settings", return_value=mock_settings),
        ):
            mock_jira = AsyncMock()
            MockJira.return_value = mock_jira
            mock_jira.get_issue = AsyncMock(return_value=mock_issue)
            mock_jira.get_labels = AsyncMock(return_value=[])
            mock_jira.get_project_repos = AsyncMock(
                side_effect=MissingProjectConfig(
                    "forge.repos for project MYPROJ is malformed: ['backend-only']"
                )
            )
            mock_jira.set_workflow_label = AsyncMock()
            mock_jira.add_comment = AsyncMock()

            MockAgent.return_value = AsyncMock()

            result = await decompose_epics(base_state)

        mock_jira.set_workflow_label.assert_called_once_with(
            "MYPROJ-1", ForgeLabel.BLOCKED
        )
        assert result["last_error"]
