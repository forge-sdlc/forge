"""Contract tests for GitHub API/webhook parsing.

These tests verify that parse_github_webhook() correctly handles
real GitHub webhook payloads. Fixtures are based on actual webhook events.
"""

import json
from pathlib import Path

from forge.integrations.github.webhooks import (
    is_ci_failure,
    is_ci_success,
    is_pr_merged,
    is_pr_review_approved,
    is_pr_review_changes_requested,
    parse_github_webhook,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "github_api_responses"


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture file."""
    with open(FIXTURES_DIR / filename) as f:
        return json.load(f)


class TestParseCheckRunEvents:
    """Test parsing of check_run webhook events."""

    def test_parse_check_run_success(self):
        """Parse a successful CI check_run event."""
        payload = load_fixture("check_run_success.json")
        data = parse_github_webhook(
            payload=payload,
            event_type="check_run",
            event_id="delivery-123",
        )

        assert data.event_id == "delivery-123"
        assert data.event_type == "check_run"
        assert data.action == "completed"
        assert data.repo_full_name == "acme/backend"
        assert data.sender_login == "github-actions[bot]"

        # Check run specific fields
        assert data.check_status == "completed"
        assert data.check_conclusion == "success"
        assert data.commit_sha == "abc123def456789012345678901234567890"

        # PR association
        assert data.pr_number == 42
        assert data.branch_name == "feature/PROJ-104-oauth"

        # Ticket extraction from branch name
        assert data.ticket_key == "PROJ-104"

        # Verify helper function
        assert is_ci_success(data) is True
        assert is_ci_failure(data) is False

    def test_parse_check_run_failure(self):
        """Parse a failed CI check_run event."""
        payload = load_fixture("check_run_failure.json")
        data = parse_github_webhook(
            payload=payload,
            event_type="check_run",
            event_id="delivery-456",
        )

        assert data.check_status == "completed"
        assert data.check_conclusion == "failure"
        assert data.pr_number == 45
        assert data.branch_name == "bugfix/PROJ-156-password-chars"
        assert data.ticket_key == "PROJ-156"

        # Verify helper functions
        assert is_ci_success(data) is False
        assert is_ci_failure(data) is True


class TestParsePullRequestEvents:
    """Test parsing of pull_request webhook events."""

    def test_parse_pull_request_opened(self):
        """Parse a pull_request opened event."""
        payload = load_fixture("pull_request_opened.json")
        data = parse_github_webhook(
            payload=payload,
            event_type="pull_request",
            event_id="delivery-789",
        )

        assert data.event_type == "pull_request"
        assert data.action == "opened"
        assert data.repo_full_name == "acme/backend"

        # PR specific fields
        assert data.pr_number == 42
        assert data.pr_url == "https://github.com/acme/backend/pull/42"
        assert data.pr_state == "open"
        assert data.branch_name == "feature/PROJ-104-oauth"

        # Ticket extraction from PR title (takes precedence over branch)
        assert data.ticket_key == "PROJ-104"

    def test_parse_pull_request_merged(self):
        """Parse a pull_request closed+merged event."""
        payload = {
            "action": "closed",
            "pull_request": {
                "number": 42,
                "state": "closed",
                "merged": True,
                "title": "PROJ-104: OAuth implementation",
                "head": {"ref": "feature/PROJ-104"},
                "html_url": "https://github.com/acme/backend/pull/42"
            },
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "senior-dev"}
        }
        data = parse_github_webhook(
            payload=payload,
            event_type="pull_request",
            event_id="delivery-merge-1",
        )

        assert data.action == "closed"
        assert data.pr_state == "closed"
        assert is_pr_merged(data) is True

    def test_parse_pull_request_closed_not_merged(self):
        """Parse a pull_request closed without merge."""
        payload = {
            "action": "closed",
            "pull_request": {
                "number": 43,
                "state": "closed",
                "merged": False,
                "title": "WIP: Experimental feature",
                "head": {"ref": "feature/experiment"},
                "html_url": "https://github.com/acme/backend/pull/43"
            },
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "dev-user"}
        }
        data = parse_github_webhook(
            payload=payload,
            event_type="pull_request",
            event_id="delivery-close-1",
        )

        assert is_pr_merged(data) is False


class TestParsePullRequestReviewEvents:
    """Test parsing of pull_request_review webhook events."""

    def test_parse_pr_review_approved(self):
        """Parse a pull_request_review approved event."""
        payload = load_fixture("pull_request_review_approved.json")
        data = parse_github_webhook(
            payload=payload,
            event_type="pull_request_review",
            event_id="delivery-review-1",
        )

        assert data.event_type == "pull_request_review"
        assert data.action == "submitted"
        assert data.pr_number == 42
        assert data.pr_url == "https://github.com/acme/backend/pull/42"
        assert data.branch_name == "feature/PROJ-104-oauth"
        assert data.ticket_key == "PROJ-104"
        assert data.sender_login == "senior-dev"

        # Verify helper function
        assert is_pr_review_approved(data) is True
        assert is_pr_review_changes_requested(data) is False

    def test_parse_pr_review_changes_requested(self):
        """Parse a pull_request_review with changes requested."""
        payload = {
            "action": "submitted",
            "review": {
                "id": 1876543211,
                "user": {"login": "senior-dev"},
                "body": "Please add error handling for the token refresh.",
                "state": "changes_requested",
                "submitted_at": "2024-03-20T15:00:00Z"
            },
            "pull_request": {
                "number": 42,
                "state": "open",
                "title": "PROJ-104: OAuth implementation",
                "head": {"ref": "feature/PROJ-104"},
                "html_url": "https://github.com/acme/backend/pull/42"
            },
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "senior-dev"}
        }
        data = parse_github_webhook(
            payload=payload,
            event_type="pull_request_review",
            event_id="delivery-review-2",
        )

        assert is_pr_review_approved(data) is False
        assert is_pr_review_changes_requested(data) is True


class TestTicketKeyExtraction:
    """Test Jira ticket key extraction from various sources."""

    def test_extract_from_pr_title(self):
        """Extract ticket from PR title."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "state": "open",
                "title": "[PROJ-123] Fix login bug",
                "head": {"ref": "fix-login"},
                "html_url": "https://github.com/org/repo/pull/1"
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user"}
        }
        data = parse_github_webhook(payload, "pull_request", "id-1")
        assert data.ticket_key == "PROJ-123"

    def test_extract_from_branch_when_title_has_no_ticket(self):
        """Fall back to branch name when title has no ticket."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "state": "open",
                "title": "Fix login bug",
                "head": {"ref": "feature/PROJ-456-login"},
                "html_url": "https://github.com/org/repo/pull/1"
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user"}
        }
        data = parse_github_webhook(payload, "pull_request", "id-2")
        assert data.ticket_key == "PROJ-456"

    def test_extract_ticket_various_formats(self):
        """Test ticket extraction with various naming conventions."""
        test_cases = [
            ("PROJ-123: Feature title", "PROJ-123"),
            ("[PROJ-123] Feature title", "PROJ-123"),
            ("Feature PROJ-123 implementation", "PROJ-123"),
            ("feat: add PROJ-123 support", "PROJ-123"),
            ("feature/PROJ-123-oauth", "PROJ-123"),
            ("bugfix/proj-456-fix", "PROJ-456"),  # Case insensitive
            ("ABC-1", "ABC-1"),  # Single digit
            ("LONGPROJECT-99999", "LONGPROJECT-99999"),  # Long project key
        ]

        for text, expected_key in test_cases:
            payload = {
                "action": "opened",
                "pull_request": {
                    "number": 1,
                    "state": "open",
                    "title": text,
                    "head": {"ref": "main"},
                    "html_url": "https://github.com/org/repo/pull/1"
                },
                "repository": {"full_name": "org/repo"},
                "sender": {"login": "user"}
            }
            data = parse_github_webhook(payload, "pull_request", "id")
            assert data.ticket_key == expected_key, f"Failed for: {text}"

    def test_no_ticket_found(self):
        """Return None when no ticket key found."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 1,
                "state": "open",
                "title": "Fix some bug",
                "head": {"ref": "fix-bug"},
                "html_url": "https://github.com/org/repo/pull/1"
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user"}
        }
        data = parse_github_webhook(payload, "pull_request", "id")
        assert data.ticket_key is None


class TestPushEvents:
    """Test parsing of push webhook events."""

    def test_parse_push_with_ticket_in_branch(self):
        """Parse a push event with ticket in branch name."""
        payload = {
            "ref": "refs/heads/feature/PROJ-789-feature",
            "after": "newcommitsha123456789012345678901234",
            "before": "oldcommitsha123456789012345678901234",
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "developer"}
        }
        data = parse_github_webhook(payload, "push", "delivery-push-1")

        assert data.event_type == "push"
        assert data.branch_name == "feature/PROJ-789-feature"
        assert data.commit_sha == "newcommitsha123456789012345678901234"
        assert data.ticket_key == "PROJ-789"


class TestEdgeCases:
    """Test edge cases and missing fields."""

    def test_minimal_payload(self):
        """Handle minimal payload with missing optional fields."""
        payload = {
            "action": "created",
            "repository": {},
            "sender": {}
        }
        data = parse_github_webhook(payload, "unknown", "id-1")

        assert data.event_type == "unknown"
        assert data.action == "created"
        assert data.repo_full_name == ""
        assert data.sender_login == ""
        assert data.ticket_key is None
        assert data.pr_number is None

    def test_check_run_without_pull_requests(self):
        """Handle check_run without associated PRs."""
        payload = {
            "action": "completed",
            "check_run": {
                "id": 123,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "sha123",
                "pull_requests": []  # No associated PRs
            },
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "bot"}
        }
        data = parse_github_webhook(payload, "check_run", "id-1")

        assert data.check_status == "completed"
        assert data.check_conclusion == "success"
        assert data.pr_number is None
        assert data.ticket_key is None

    def test_raw_payload_preserved(self):
        """Verify raw payload is preserved in parsed data."""
        payload = {
            "action": "opened",
            "custom_field": "custom_value",
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "user"}
        }
        data = parse_github_webhook(payload, "test", "id-1")

        assert data.raw_payload == payload
        assert data.raw_payload["custom_field"] == "custom_value"


class TestIssueCommentEvents:
    """Test parsing of issue_comment events (PR comments)."""

    def test_parse_pr_comment(self):
        """Parse a comment on a pull request."""
        payload = {
            "action": "created",
            "issue": {
                "number": 42,
                "title": "PROJ-104: OAuth implementation",
                "html_url": "https://github.com/acme/backend/pull/42",
                "pull_request": {
                    "url": "https://api.github.com/repos/acme/backend/pulls/42"
                }
            },
            "comment": {
                "id": 12345,
                "body": "Looks good, just one question..."
            },
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "reviewer"}
        }
        data = parse_github_webhook(payload, "issue_comment", "id-1")

        assert data.event_type == "issue_comment"
        assert data.action == "created"
        assert data.pr_number == 42
        assert data.pr_url == "https://github.com/acme/backend/pull/42"
        assert data.ticket_key == "PROJ-104"

    def test_parse_issue_comment_not_pr(self):
        """Parse a comment on a regular issue (not a PR)."""
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "Bug report",
                "html_url": "https://github.com/acme/backend/issues/100"
                # No pull_request field
            },
            "comment": {
                "id": 12346,
                "body": "Can you provide more details?"
            },
            "repository": {"full_name": "acme/backend"},
            "sender": {"login": "maintainer"}
        }
        data = parse_github_webhook(payload, "issue_comment", "id-2")

        assert data.event_type == "issue_comment"
        # Should not set PR fields since this is not a PR
        assert data.pr_number is None
        assert data.ticket_key is None
