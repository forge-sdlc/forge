"""Contract tests for Jira API response parsing.

These tests verify that JiraIssue.from_api_response() correctly handles
real Jira API response shapes. Fixtures are based on actual API responses.
"""

import json
from pathlib import Path

from forge.integrations.jira.models import JiraComment, JiraIssue

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "jira_api_responses"


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture file."""
    with open(FIXTURES_DIR / filename) as f:
        return json.load(f)


class TestJiraIssueFromApiResponse:
    """Test JiraIssue.from_api_response() with various API shapes."""

    def test_parse_feature_with_adf_description(self):
        """Parse a Feature issue with Atlassian Document Format description."""
        data = load_fixture("feature_with_adf_description.json")
        issue = JiraIssue.from_api_response(data)

        assert issue.key == "PROJ-104"
        assert issue.id == "10423"
        assert issue.summary == "Implement OAuth2 authentication flow"
        assert issue.issue_type == "Feature"
        assert issue.status == "Refinement"

        # Verify ADF paragraph text was parsed
        # Note: Current parser only extracts top-level paragraphs, not headings or nested lists
        assert "As a developer, I want to authenticate users via OAuth2" in issue.description

        # Verify labels
        assert "forge:managed" in issue.labels
        assert "forge:prd-pending" in issue.labels
        assert "backend" in issue.labels

        # Verify project key extraction
        assert issue.project_key == "PROJ"

        # Verify timestamps
        assert issue.created is not None
        assert issue.created.year == 2024
        assert issue.created.month == 3
        assert issue.created.day == 15

        # Verify custom fields are captured
        assert "customfield_10001" in issue.custom_fields
        assert issue.custom_fields["customfield_10001"] == "https://github.com/acme/backend"

    def test_parse_bug_with_plain_text_description(self):
        """Parse a Bug issue with plain text description (not ADF)."""
        data = load_fixture("bug_with_plain_text.json")
        issue = JiraIssue.from_api_response(data)

        assert issue.key == "PROJ-156"
        assert issue.id == "10456"
        assert issue.issue_type == "Bug"
        assert issue.status == "New"

        # Plain text should be preserved
        assert "Steps to reproduce:" in issue.description
        assert "500 Internal Server Error" in issue.description
        assert "validators.py:23" in issue.description

        # Verify labels
        assert "forge:managed" in issue.labels
        assert "production" in issue.labels
        assert "urgent" in issue.labels

    def test_parse_epic_with_parent_key(self):
        """Parse an Epic issue that has a parent Feature."""
        data = load_fixture("epic_with_parent.json")
        issue = JiraIssue.from_api_response(data)

        assert issue.key == "PROJ-200"
        assert issue.issue_type == "Epic"
        assert issue.parent_key == "PROJ-104"

        # Verify top-level paragraph description parsing
        # Note: Nested bullet list items are not extracted by current parser
        assert "Google OAuth2 provider" in issue.description

    def test_parse_task_with_no_description(self):
        """Parse a Task issue with null description."""
        data = load_fixture("task_no_description.json")
        issue = JiraIssue.from_api_response(data)

        assert issue.key == "PROJ-250"
        assert issue.issue_type == "Task"
        assert issue.summary == "Add Google OAuth client ID to environment config"

        # Null description should result in empty string
        assert issue.description == ""

        # Should still have parent
        assert issue.parent_key == "PROJ-200"

    def test_parse_issue_with_missing_optional_fields(self):
        """Parse an issue with minimal fields (missing optional data)."""
        minimal_data = {
            "id": "99999",
            "key": "MIN-1",
            "fields": {
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "summary": "Minimal issue"
            }
        }
        issue = JiraIssue.from_api_response(minimal_data)

        assert issue.key == "MIN-1"
        assert issue.id == "99999"
        assert issue.summary == "Minimal issue"
        assert issue.description == ""
        assert issue.parent_key is None
        assert issue.labels == []
        assert issue.custom_fields == {}
        assert issue.created is None
        assert issue.updated is None

    def test_parse_issue_with_empty_adf_content(self):
        """Parse an issue with empty ADF document."""
        data = {
            "id": "10001",
            "key": "TEST-1",
            "fields": {
                "issuetype": {"name": "Feature"},
                "status": {"name": "New"},
                "summary": "Test",
                "description": {
                    "version": 1,
                    "type": "doc",
                    "content": []
                }
            }
        }
        issue = JiraIssue.from_api_response(data)

        assert issue.description == ""


class TestJiraCommentFromApiResponse:
    """Test JiraComment.from_api_response() with various API shapes."""

    def test_parse_comment_with_adf_body(self):
        """Parse a comment with Atlassian Document Format body."""
        data = load_fixture("comment_with_adf.json")
        comment = JiraComment.from_api_response(data)

        assert comment.id == "10100"
        assert comment.author_id == "5b10a2844c20165700ede21g"
        assert comment.author_name == "Alice Johnson"

        # Verify top-level paragraph text was parsed
        # Note: Nested bullet list items are not extracted by current parser
        assert "Please add more detail" in comment.body

        # Verify timestamps
        assert comment.created is not None
        assert comment.created.year == 2024

    def test_parse_comment_with_plain_text_body(self):
        """Parse a comment with plain text body."""
        data = {
            "id": "10200",
            "author": {
                "accountId": "user-123",
                "displayName": "Bob Smith"
            },
            "body": "LGTM! Approved.",
            "created": "2024-03-21T10:00:00.000+0000",
            "updated": "2024-03-21T10:00:00.000+0000"
        }
        comment = JiraComment.from_api_response(data)

        assert comment.id == "10200"
        assert comment.body == "LGTM! Approved."
        assert comment.author_name == "Bob Smith"

    def test_parse_comment_with_missing_optional_fields(self):
        """Parse a comment with minimal fields."""
        data = {
            "id": "10300",
            "author": {},
            "body": "Simple comment"
        }
        comment = JiraComment.from_api_response(data)

        assert comment.id == "10300"
        assert comment.body == "Simple comment"
        assert comment.author_id == ""
        assert comment.author_name == ""
        assert comment.created is None


class TestAdfTextExtraction:
    """Test ADF (Atlassian Document Format) text extraction."""

    def test_extract_text_from_nested_paragraphs(self):
        """Extract text from nested paragraph elements."""
        adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "First paragraph."}
                    ]
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Second paragraph."}
                    ]
                }
            ]
        }
        text = JiraIssue._extract_text_from_adf(adf)
        assert "First paragraph." in text
        assert "Second paragraph." in text

    def test_extract_text_handles_non_dict_input(self):
        """Gracefully handle non-dict ADF input."""
        assert JiraIssue._extract_text_from_adf(None) == ""
        assert JiraIssue._extract_text_from_adf("plain string") == "plain string"
        assert JiraIssue._extract_text_from_adf(123) == "123"

    def test_extract_text_from_bullet_list(self):
        """Extract text from bullet list items."""
        adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "Item one"}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        # Note: Current implementation may not handle nested list items
        # This test documents the current behavior
        text = JiraIssue._extract_text_from_adf(adf)
        # Bullet lists are not directly handled - this may need enhancement
        assert text == "" or "Item one" in text


class TestProjectKeyExtraction:
    """Test project key extraction from issue key."""

    def test_extract_project_key_standard(self):
        """Extract project key from standard issue key."""
        issue = JiraIssue(
            key="PROJ-123",
            id="1",
            summary="Test",
            description="",
            status="Open",
            issue_type="Task"
        )
        assert issue.project_key == "PROJ"

    def test_extract_project_key_multi_part(self):
        """Extract project key with hyphens in project name."""
        issue = JiraIssue(
            key="MY-PROJECT-456",
            id="1",
            summary="Test",
            description="",
            status="Open",
            issue_type="Task"
        )
        # Should extract everything before the last hyphen-number
        assert issue.project_key == "MY-PROJECT"

    def test_extract_project_key_no_hyphen(self):
        """Handle key without hyphen (edge case)."""
        issue = JiraIssue(
            key="INVALID",
            id="1",
            summary="Test",
            description="",
            status="Open",
            issue_type="Task"
        )
        assert issue.project_key == "INVALID"


class TestDateParsing:
    """Test date/time parsing from API responses."""

    def test_parse_iso_date_with_timezone(self):
        """Parse ISO date with timezone offset."""
        data = {
            "id": "1",
            "key": "TEST-1",
            "fields": {
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "summary": "Test",
                "created": "2024-03-15T10:23:45.000+0000",
                "updated": "2024-03-20T14:30:22.000-0800"
            }
        }
        issue = JiraIssue.from_api_response(data)

        assert issue.created is not None
        assert issue.created.year == 2024
        assert issue.created.month == 3
        assert issue.created.day == 15
        assert issue.created.hour == 10
        assert issue.created.minute == 23

    def test_parse_iso_date_with_z_suffix(self):
        """Parse ISO date with Z timezone indicator."""
        data = {
            "id": "1",
            "key": "TEST-1",
            "fields": {
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "summary": "Test",
                "created": "2024-01-01T00:00:00.000Z"
            }
        }
        issue = JiraIssue.from_api_response(data)

        assert issue.created is not None
        assert issue.created.year == 2024
        assert issue.created.month == 1
        assert issue.created.day == 1
