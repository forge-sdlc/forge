"""Unit tests for Jira client."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from forge.integrations.jira.client import JiraClient, MissingProjectConfig
from forge.integrations.jira.models import JiraIssue
from forge.models.workflow import ForgeLabel


class TestJiraClientInit:
    """Tests for JiraClient initialization."""

    def test_creates_with_default_settings(self):
        """Client creates with default settings."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()

            assert client.base_url == "https://test.atlassian.net/rest/api/3"

    def test_creates_with_custom_settings(self):
        """Client creates with custom settings."""
        from forge.config import Settings

        settings = Settings(
            jira_base_url="https://custom.atlassian.net",
            jira_api_token="custom-token",
            jira_user_email="custom@example.com",
            jira_webhook_secret="secret",
            redis_url="redis://localhost:6379",
            github_token="token",
            github_webhook_secret="secret",
            llm_backend="anthropic",
            llm_model="claude-sonnet-4-5-20250929",
            anthropic_api_key="key",
        )

        client = JiraClient(settings=settings)

        assert "custom.atlassian.net" in client.base_url


class TestJiraClientGetIssue:
    """Tests for get_issue method."""

    @pytest.fixture
    def mock_client(self):
        """Create client with mocked HTTP."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()
            return client

    @pytest.mark.asyncio
    async def test_get_issue_success(self, mock_client):
        """Successfully retrieves issue."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "10001",
            "key": "TEST-123",
            "fields": {
                "issuetype": {"name": "Feature"},
                "status": {"name": "New"},
                "summary": "Test Issue",
                "description": {"type": "doc", "content": []},
                "labels": ["forge:managed"],
                "project": {"key": "TEST"},
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.request = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            issue = await mock_client.get_issue("TEST-123")

        assert issue.key == "TEST-123"
        assert issue.issue_type == "Feature"


class TestJiraClientStructuredComments:
    """Tests for structured artifact comments."""

    @pytest.fixture
    def jira_client(self):
        """Create client with mocked settings."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"
            return JiraClient()

    @pytest.mark.asyncio
    async def test_add_structured_comment_includes_interaction_options_outside_marker(
        self, jira_client
    ):
        """Artifact comments include actions without polluting stored content."""
        jira_client.add_comment = AsyncMock(return_value=MagicMock())

        await jira_client.add_structured_comment(
            "TEST-123",
            "Product Requirements Document (PRD)",
            "# PRD\n\nGenerated content.",
            comment_type="prd",
        )

        body = jira_client.add_comment.call_args.args[1]
        assert body.startswith("[FORGE:PRD]\n# Product Requirements Document (PRD)")
        assert "[/FORGE:PRD]\n\n## 🤖 Forge interaction options" in body
        assert "**Approve:** add `forge:prd-approved` to continue." in body
        assert "**Request changes:** add a Jira comment starting with `!`" in body
        assert "**Ask a question:** add a Jira comment starting with `?`." in body
        assert "@forge ask" not in body
        marker_end = body.index("[/FORGE:PRD]")
        assert "## 🤖 Forge interaction options" not in body[:marker_end]


class TestJiraClientComments:
    """Tests for paginated comment retrieval."""

    @pytest.mark.asyncio
    async def test_get_comments_reads_marker_from_later_page(self):
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"
            jira = JiraClient()

        first_response = MagicMock()
        first_response.json.return_value = {
            "startAt": 0,
            "maxResults": 1,
            "total": 2,
            "comments": [{"id": "1", "body": "Older comment"}],
        }
        second_response = MagicMock()
        second_response.json.return_value = {
            "startAt": 1,
            "maxResults": 1,
            "total": 2,
            "comments": [{"id": "2", "body": "Event/correlation ID: evt-terminal-1"}],
        }
        http = AsyncMock()
        http.get = AsyncMock(side_effect=[first_response, second_response])

        with patch.object(jira, "_get_client", return_value=http):
            comments = await jira.get_comments("TEST-123")

        assert [comment.id for comment in comments] == ["1", "2"]
        assert "evt-terminal-1" in comments[1].body
        assert http.get.await_args_list[0].kwargs["params"]["startAt"] == 0
        assert http.get.await_args_list[1].kwargs["params"]["startAt"] == 1


class TestJiraClientLabels:
    """Tests for label operations."""

    @pytest.fixture
    def mock_client(self):
        """Create client with mocked HTTP."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()
            return client

    @pytest.mark.asyncio
    async def test_get_labels(self, mock_client):
        """Successfully retrieves labels."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "fields": {"labels": ["forge:managed", "forge:prd-pending"]}
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            labels = await mock_client.get_labels("TEST-123")

        assert "forge:managed" in labels
        assert "forge:prd-pending" in labels

    @pytest.mark.asyncio
    async def test_add_labels(self, mock_client):
        """Successfully adds labels."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.put = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            await mock_client.add_labels("TEST-123", ["new-label"])

        mock_http.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_workflow_label_removes_old_forge_labels(self, mock_client):
        """set_workflow_label removes old forge labels."""
        # Mock get_labels to return current labels
        mock_client.get_labels = AsyncMock(
            return_value=["forge:managed", "forge:prd-pending", "other-label"]
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.put = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            await mock_client.set_workflow_label("TEST-123", ForgeLabel.PRD_APPROVED)

        # Check that the PUT was called with correct operations
        mock_http.put.assert_called_once()
        call_args = mock_http.put.call_args
        update_ops = call_args.kwargs["json"]["update"]["labels"]

        # Should remove prd-pending and add prd-approved
        remove_ops = [op for op in update_ops if "remove" in op]
        add_ops = [op for op in update_ops if "add" in op]

        assert any(op["remove"] == "forge:prd-pending" for op in remove_ops)
        assert any(op["add"] == ForgeLabel.PRD_APPROVED.value for op in add_ops)


class TestJiraClientArchiveIssue:
    """Tests for archive_issue method."""

    @pytest.fixture
    def mock_client(self):
        """Create client with mocked settings."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()
            return client

    @pytest.mark.asyncio
    async def test_archive_issue_updates_labels_unlinks_parent_and_archives_natively(
        self, mock_client
    ):
        """archive_issue cleans labels, unlinks parent, and calls native archive endpoint."""
        issue = MagicMock()
        issue.labels = ["forge:managed", "forge:task-pending", "repo:acme/backend"]
        mock_client.get_issue = AsyncMock(return_value=issue)

        label_response = MagicMock()
        label_response.raise_for_status = MagicMock()
        parent_response = MagicMock()
        parent_response.raise_for_status = MagicMock()
        archive_response = MagicMock()
        archive_response.raise_for_status = MagicMock()
        archive_response.json.return_value = {"errors": {}, "numberOfIssuesUpdated": 1}

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.put = AsyncMock(
                side_effect=[label_response, parent_response, archive_response]
            )
            mock_get_client.return_value = mock_http

            await mock_client.archive_issue("TEST-123", archive_subtasks=False)

        assert mock_http.put.await_args_list[0].args[0] == "/issue/TEST-123"
        label_ops = mock_http.put.await_args_list[0].kwargs["json"]["update"]["labels"]
        assert {"remove": "forge:managed"} in label_ops
        assert {"remove": "forge:task-pending"} in label_ops
        assert {"add": "forge:archived"} in label_ops

        assert mock_http.put.await_args_list[1].args[0] == "/issue/TEST-123"
        assert mock_http.put.await_args_list[1].kwargs["json"] == {"fields": {"parent": None}}

        assert mock_http.put.await_args_list[2].args[0] == "/issue/archive"
        assert mock_http.put.await_args_list[2].kwargs["json"] == {"issueIdsOrKeys": ["TEST-123"]}

    @pytest.mark.asyncio
    async def test_archive_issue_logs_native_archive_body_errors(self, mock_client, caplog):
        """Jira may return HTTP 200 with per-issue archive failures in the body."""
        import logging

        issue = MagicMock()
        issue.labels = ["forge:managed"]
        mock_client.get_issue = AsyncMock(return_value=issue)

        label_response = MagicMock()
        label_response.raise_for_status = MagicMock()
        parent_response = MagicMock()
        parent_response.raise_for_status = MagicMock()
        archive_response = MagicMock()
        archive_response.raise_for_status = MagicMock()
        archive_response.json.return_value = {
            "errors": {
                "issueIsSubtask": {
                    "count": 1,
                    "issueIdsOrKeys": ["TEST-123"],
                    "message": "Issue is subtask.",
                }
            },
            "numberOfIssuesUpdated": 0,
        }

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.put = AsyncMock(
                side_effect=[label_response, parent_response, archive_response]
            )
            mock_get_client.return_value = mock_http

            with caplog.at_level(logging.WARNING, logger="forge.integrations.jira.client"):
                await mock_client.archive_issue("TEST-123", archive_subtasks=False)

        assert any(
            "Failed to natively archive TEST-123" in record.message
            and "Issue is subtask." in record.message
            for record in caplog.records
        )


class TestJiraClientErrorComments:
    """Tests for error comment safety."""

    @pytest.fixture
    def mock_client(self):
        """Create client with mocked settings."""
        with patch("forge.integrations.jira.client.get_settings") as mock_settings:
            mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
            mock_settings.return_value.jira_api_token = MagicMock()
            mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
            mock_settings.return_value.jira_user_email = "test@example.com"

            client = JiraClient()
            return client

    @pytest.mark.asyncio
    async def test_add_error_comment_redacts_authenticated_git_urls(self, mock_client):
        """Error comments must not include GitHub tokens from git command errors."""
        token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456"
        raw_url = f"https://x-access-token:{token}@github.com/org/repo.git"
        error_message = f"Command '['git', 'clone', '{raw_url}']' failed"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id": "10000",
            "body": "ok",
            "author": {"accountId": "bot", "displayName": "Bot"},
        }

        with patch.object(mock_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            await mock_client.add_error_comment(
                issue_key="TEST-123",
                error_message=error_message,
                node_name="setup_workspace",
            )

        body = mock_http.post.await_args.kwargs["json"]["body"]
        posted_text = str(body)
        assert "ghp_" not in posted_text
        assert raw_url not in posted_text
        assert "https://[REDACTED]@github.com/org/repo.git" in posted_text


class TestJiraClientADF:
    """Tests for ADF conversion."""

    def test_text_to_adf_simple_paragraph(self):
        """Simple text converts to ADF paragraph."""
        text = "This is a simple paragraph."

        adf = JiraClient._text_to_adf(text)

        assert adf["type"] == "doc"
        assert adf["version"] == 1
        assert len(adf["content"]) >= 1

    def test_text_to_adf_heading(self):
        """Markdown heading converts to ADF heading."""
        text = "# Heading 1"

        adf = JiraClient._text_to_adf(text)

        heading = adf["content"][0]
        assert heading["type"] == "heading"
        assert heading["attrs"]["level"] == 1

    def test_text_to_adf_bullet_list(self):
        """Markdown bullet list converts to ADF."""
        text = "- Item 1\n- Item 2\n- Item 3"

        adf = JiraClient._text_to_adf(text)

        bullet_list = adf["content"][0]
        assert bullet_list["type"] == "bulletList"
        assert len(bullet_list["content"]) == 3

    def test_text_to_adf_code_block(self):
        """Markdown code block converts to ADF."""
        text = "```python\ndef hello():\n    print('world')\n```"

        adf = JiraClient._text_to_adf(text)

        code_block = adf["content"][0]
        assert code_block["type"] == "codeBlock"

    def test_text_to_adf_inline_formatting(self):
        """Inline formatting converts correctly."""
        text = "This has **bold** and *italic* text."

        adf = JiraClient._text_to_adf(text)

        # Should have paragraph with inline marks
        assert adf["content"][0]["type"] == "paragraph"

    def test_text_to_adf_links_bare_urls(self):
        text = "Created: https://github.com/forge-sdlc/forge/pull/293."

        adf = JiraClient._text_to_adf(text)

        paragraph = adf["content"][0]["content"]
        link = next(node for node in paragraph if node.get("marks"))
        assert link["text"] == "https://github.com/forge-sdlc/forge/pull/293"
        assert link["marks"] == [
            {
                "type": "link",
                "attrs": {"href": "https://github.com/forge-sdlc/forge/pull/293"},
            }
        ]
        assert paragraph[-1]["text"] == "."

    def test_text_to_adf_does_not_link_urls_in_code_blocks(self):
        adf = JiraClient._text_to_adf("```\nhttps://example.com/script\n```")

        code_text = adf["content"][0]["content"][0]
        assert "marks" not in code_text

    def test_text_to_adf_does_not_link_urls_in_inline_code(self):
        adf = JiraClient._text_to_adf("Run `curl https://example.com/api` locally.")

        paragraph = adf["content"][0]["content"]
        code_node = next(
            node
            for node in paragraph
            if any(mark.get("type") == "code" for mark in node.get("marks", []))
        )
        # The URL stays inside the inline-code node without a link mark.
        assert "https://example.com/api" in code_node["text"]
        assert all(mark.get("type") != "link" for mark in code_node.get("marks", []))

    def test_issue_description_extracts_epic_plan_blocks(self):
        """Epic plan descriptions survive the markdown to ADF to JiraIssue round trip."""
        plan = """# Task Takeover Routing

- Keep workflow identity in TaskTakeoverWorkflow.matches().
- Preserve forge:managed as the user-facing workflow opt-in.

```python
def matches(issue):
    return True
```"""
        adf = JiraClient._text_to_adf(plan)

        issue = JiraIssue.from_api_response(
            {
                "key": "AISOS-1981",
                "id": "10001",
                "fields": {
                    "summary": "Task Takeover Routing",
                    "description": adf,
                    "status": {"name": "To Do"},
                    "issuetype": {"name": "Epic"},
                },
            }
        )

        assert "# Task Takeover Routing" in issue.description
        assert "- Keep workflow identity" in issue.description
        assert "- Preserve forge:managed" in issue.description
        assert "```python" in issue.description
        assert "def matches(issue):" in issue.description

    def test_issue_description_preserves_unparsed_adf_node(self):
        """Unknown ADF nodes should not disappear from issue descriptions."""
        issue = JiraIssue.from_api_response(
            {
                "key": "AISOS-1981",
                "id": "10001",
                "fields": {
                    "summary": "Unknown node",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "unsupportedWidget",
                                "attrs": {"payload": "important raw context"},
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "issuetype": {"name": "Epic"},
                },
            }
        )

        assert "unsupportedWidget" in issue.description
        assert "important raw context" in issue.description


@pytest.fixture
def jira_client():
    """Jira client with mocked settings."""
    with patch("forge.integrations.jira.client.get_settings") as mock_settings:
        mock_settings.return_value.jira_base_url = "https://test.atlassian.net"
        mock_settings.return_value.jira_api_token = MagicMock()
        mock_settings.return_value.jira_api_token.get_secret_value.return_value = "token"
        mock_settings.return_value.jira_user_email = "test@example.com"
        yield JiraClient()


class TestGetProjectProperty:
    """Tests for get_project_property method."""

    @pytest.mark.asyncio
    async def test_returns_value_on_success(self, jira_client):
        """Returns the property value when the API responds with 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "forge.repos", "value": ["acme/backend"]}
        mock_response.raise_for_status = MagicMock()

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_project_property("MYPROJ", "forge.repos")

        assert result == ["acme/backend"]

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, jira_client):
        """Returns None when the property is not set (404)."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_project_property("MYPROJ", "forge.repos")

        assert result is None


class TestGetProjectRepos:
    """Tests for get_project_repos method."""

    @pytest.mark.asyncio
    async def test_returns_repo_list_on_success(self, jira_client):
        """Returns list of repos when forge.repos is set correctly."""
        jira_client.get_project_property = AsyncMock(return_value=["acme/backend", "acme/frontend"])

        result = await jira_client.get_project_repos("MYPROJ")

        assert result == ["acme/backend", "acme/frontend"]

    @pytest.mark.asyncio
    async def test_raises_on_missing_property(self, jira_client):
        """Raises MissingProjectConfig when forge.repos is not set."""
        jira_client.get_project_property = AsyncMock(return_value=None)

        with pytest.raises(MissingProjectConfig, match="forge.repos not set"):
            await jira_client.get_project_repos("MYPROJ")

    @pytest.mark.asyncio
    async def test_raises_on_not_a_list(self, jira_client):
        """Raises MissingProjectConfig when forge.repos is not a list."""
        jira_client.get_project_property = AsyncMock(return_value="acme/backend")

        with pytest.raises(MissingProjectConfig, match="malformed"):
            await jira_client.get_project_repos("MYPROJ")

    @pytest.mark.asyncio
    async def test_raises_on_entry_without_slash(self, jira_client):
        """Raises MissingProjectConfig when a repo entry lacks owner/repo format."""
        jira_client.get_project_property = AsyncMock(return_value=["backend-only"])

        with pytest.raises(MissingProjectConfig, match="malformed"):
            await jira_client.get_project_repos("MYPROJ")


class TestGetProjectDefaultRepo:
    """Tests for get_project_default_repo method."""

    @pytest.mark.asyncio
    async def test_returns_repo_string_on_success(self, jira_client):
        """Returns repo string when forge.default_repo is set correctly."""
        jira_client.get_project_property = AsyncMock(return_value="acme/backend")

        result = await jira_client.get_project_default_repo("MYPROJ")

        assert result == "acme/backend"

    @pytest.mark.asyncio
    async def test_raises_on_missing_property(self, jira_client):
        """Raises MissingProjectConfig when forge.default_repo is not set."""
        jira_client.get_project_property = AsyncMock(return_value=None)

        with pytest.raises(MissingProjectConfig, match="forge.default_repo not set"):
            await jira_client.get_project_default_repo("MYPROJ")

    @pytest.mark.asyncio
    async def test_raises_on_not_a_string(self, jira_client):
        """Raises MissingProjectConfig when forge.default_repo is not a string."""
        jira_client.get_project_property = AsyncMock(return_value=["acme/backend"])

        with pytest.raises(MissingProjectConfig, match="malformed"):
            await jira_client.get_project_default_repo("MYPROJ")

    @pytest.mark.asyncio
    async def test_raises_on_string_without_slash(self, jira_client):
        """Raises MissingProjectConfig when forge.default_repo lacks owner/repo format."""
        jira_client.get_project_property = AsyncMock(return_value="backend-only")

        with pytest.raises(MissingProjectConfig, match="malformed"):
            await jira_client.get_project_default_repo("MYPROJ")


class TestGetSkillsConfig:
    """Tests for get_skills_config method."""

    def _make_response(self, status_code: int, json_data: Any = None) -> MagicMock:
        """Helper to build a mock HTTP response."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        if json_data is not None:
            mock_response.json.return_value = json_data
        mock_response.raise_for_status = MagicMock()
        return mock_response

    @pytest.mark.asyncio
    async def test_returns_none_when_property_not_set(self, jira_client):
        """Returns None when forge.skills is not set (404)."""
        mock_response = self._make_response(404)

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_skill_entries_for_valid_list(self, jira_client):
        """Returns list of SkillEntry when forge.skills contains a valid list."""
        from forge.skills.models import SkillEntry

        mock_response = self._make_response(
            200,
            {
                "value": [
                    {"source": "https://github.com/acme/skills", "ref": "main", "path": "skill_0"},
                ]
            },
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], SkillEntry)
        assert result[0].source == "https://github.com/acme/skills"
        assert result[0].ref == "main"
        assert result[0].path == "skill_0"

    @pytest.mark.asyncio
    async def test_returns_skill_entries_with_skill_mapping(self, jira_client):
        """Returns SkillEntry with skill_mapping mode."""
        from forge.skills.models import SkillEntry

        mock_response = self._make_response(
            200,
            {
                "value": [
                    {
                        "source": "https://github.com/acme/skills",
                        "ref": "v1.0.0",
                        "skill_mapping": {"my-skill": "skills/my_skill"},
                    },
                ]
            },
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], SkillEntry)
        assert result[0].skill_mapping == {"my-skill": "skills/my_skill"}

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_invalid_json_string(self, jira_client):
        """Returns empty list and logs warning when value is a malformed JSON string."""
        mock_response = self._make_response(200, {"value": "not-valid-json!!!"})

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_non_list_value(self, jira_client):
        """Returns empty list when forge.skills value is not a list."""
        mock_response = self._make_response(
            200, {"value": {"source": "https://github.com/x/y", "path": "a"}}
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_invalid_skill_entry(self, jira_client):
        """Returns empty list when an entry fails SkillEntry validation."""
        mock_response = self._make_response(
            200,
            {
                "value": [
                    # Missing required 'source' field, and has both path and skill_mapping
                    {"path": "skills/", "skill_mapping": {"x": "y"}},
                ]
            },
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_empty_list(self, jira_client):
        """Returns empty list when forge.skills is set to an empty array."""
        mock_response = self._make_response(200, {"value": []})

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result == []

    @pytest.mark.asyncio
    async def test_parses_json_string_value(self, jira_client):
        """Handles when Jira returns the value as a raw JSON string."""
        import json

        from forge.skills.models import SkillEntry

        raw = json.dumps([{"source": "https://github.com/acme/skills", "path": "skills/"}])
        mock_response = self._make_response(200, {"value": raw})

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.get_skills_config("MYPROJ")

        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], SkillEntry)
        assert result[0].source == "https://github.com/acme/skills"


class TestJiraClientListProjectProperties:
    """Tests for list_project_properties method."""

    @pytest.mark.asyncio
    async def test_success_case(self, jira_client):
        """Successfully parses valid project properties."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "keys": [{"key": "forge.repos", "self": "https://..."}, {"key": "forge.default_repo"}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result = await jira_client.list_project_properties("MYPROJ")

        assert result == ["forge.repos", "forge.default_repo"]
        mock_http.get.assert_called_once_with("/project/MYPROJ/properties")

    @pytest.mark.asyncio
    async def test_empty_properties_list(self, jira_client):
        """Returns empty list when response keys list is empty or missing."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"keys": []}
        mock_response.raise_for_status = MagicMock()

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result1 = await jira_client.list_project_properties("MYPROJ")

        assert result1 == []

        # Missing keys key completely
        mock_response.json.return_value = {}
        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            result2 = await jira_client.list_project_properties("MYPROJ")

        assert result2 == []

    @pytest.mark.asyncio
    async def test_nonexistent_project_404(self, jira_client):
        """Raises httpx.HTTPStatusError on 404 response."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 404
        request = httpx.Request(
            "GET", "https://test.atlassian.net/rest/api/3/project/MYPROJ/properties"
        )
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found", request=request, response=mock_response
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            with pytest.raises(httpx.HTTPStatusError):
                await jira_client.list_project_properties("MYPROJ")

    @pytest.mark.asyncio
    async def test_authorization_failure_403(self, jira_client):
        """Raises httpx.HTTPStatusError on 403 response."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 403
        request = httpx.Request(
            "GET", "https://test.atlassian.net/rest/api/3/project/MYPROJ/properties"
        )
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=request, response=mock_response
        )

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            with pytest.raises(httpx.HTTPStatusError):
                await jira_client.list_project_properties("MYPROJ")

    @pytest.mark.asyncio
    async def test_malformed_response(self, jira_client):
        """Raises ValueError if the response is malformed."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        import json

        mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
        mock_response.raise_for_status = MagicMock()

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http

            with pytest.raises(ValueError, match="Malformed Jira project properties response"):
                await jira_client.list_project_properties("MYPROJ")

        # Non-dictionary key in list
        mock_response2 = MagicMock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = {"keys": ["not-a-dict"]}
        mock_response2.raise_for_status = MagicMock()

        with patch.object(jira_client, "_get_client") as mock_get_client:
            mock_http2 = AsyncMock()
            mock_http2.get = AsyncMock(return_value=mock_response2)
            mock_get_client.return_value = mock_http2

            result = await jira_client.list_project_properties("MYPROJ")
            assert result == []
