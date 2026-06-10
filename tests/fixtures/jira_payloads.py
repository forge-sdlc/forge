"""Jira webhook and API response fixtures for testing."""

from copy import deepcopy
from typing import Any

# Sample Jira webhook: issue_created
WEBHOOK_ISSUE_CREATED: dict[str, Any] = {
    "timestamp": 1711814400000,
    "webhookEvent": "jira:issue_created",
    "issue_event_type_name": "issue_created",
    "user": {
        "accountId": "user-123",
        "displayName": "Test User",
        "emailAddress": "test@example.com",
    },
    "issue": {
        "id": "10001",
        "key": "TEST-123",
        "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        "fields": {
            "issuetype": {"name": "Feature", "id": "10001"},
            "project": {"key": "TEST", "name": "Test Project"},
            "status": {"name": "New", "id": "1"},
            "summary": "Test Feature",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Raw requirements for testing"}],
                    }
                ],
            },
            "labels": ["forge:managed"],
            "created": "2024-03-30T10:00:00.000+0000",
            "updated": "2024-03-30T10:00:00.000+0000",
        },
    },
}

# Sample Jira webhook: label added (triggers workflow)
WEBHOOK_ISSUE_UPDATED_LABEL_ADDED: dict[str, Any] = {
    "timestamp": 1711814500000,
    "webhookEvent": "jira:issue_updated",
    "issue_event_type_name": "issue_generic",
    "user": {
        "accountId": "user-123",
        "displayName": "Test User",
        "emailAddress": "test@example.com",
    },
    "issue": {
        "id": "10001",
        "key": "TEST-123",
        "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        "fields": {
            "issuetype": {"name": "Feature", "id": "10001"},
            "project": {"key": "TEST", "name": "Test Project"},
            "status": {"name": "New", "id": "1"},
            "summary": "Test Feature",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "PRD content here"}],
                    }
                ],
            },
            "labels": ["forge:managed", "forge:prd-approved"],
            "created": "2024-03-30T10:00:00.000+0000",
            "updated": "2024-03-30T10:05:00.000+0000",
        },
    },
    "changelog": {
        "id": "12345",
        "items": [
            {
                "field": "labels",
                "fieldtype": "jira",
                "from": None,
                "fromString": "forge:managed forge:prd-pending",
                "to": None,
                "toString": "forge:managed forge:prd-approved",
            }
        ],
    },
}

# Sample Jira webhook: comment added (triggers revision)
WEBHOOK_ISSUE_UPDATED_COMMENT_ADDED: dict[str, Any] = {
    "timestamp": 1711814600000,
    "webhookEvent": "jira:issue_updated",
    "issue_event_type_name": "issue_commented",
    "user": {
        "accountId": "user-456",
        "displayName": "PM User",
        "emailAddress": "pm@example.com",
    },
    "issue": {
        "id": "10001",
        "key": "TEST-123",
        "self": "https://test.atlassian.net/rest/api/3/issue/10001",
        "fields": {
            "issuetype": {"name": "Feature", "id": "10001"},
            "project": {"key": "TEST", "name": "Test Project"},
            "status": {"name": "New", "id": "1"},
            "summary": "Test Feature",
            "labels": ["forge:managed", "forge:prd-pending"],
        },
    },
    "comment": {
        "id": "10100",
        "author": {
            "accountId": "user-456",
            "displayName": "PM User",
        },
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Please add more detail about the user persona.",
                        }
                    ],
                }
            ],
        },
        "created": "2024-03-30T10:10:00.000+0000",
    },
}

# Sample Jira API GET /issue response for Feature
API_GET_ISSUE_FEATURE: dict[str, Any] = {
    "id": "10001",
    "key": "TEST-123",
    "self": "https://test.atlassian.net/rest/api/3/issue/10001",
    "fields": {
        "issuetype": {"name": "Feature", "id": "10001"},
        "project": {"key": "TEST", "name": "Test Project", "id": "10000"},
        "status": {"name": "New", "id": "1"},
        "summary": "Test Feature: User authentication",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "As a user, I want to log in with my email and password.",
                        }
                    ],
                }
            ],
        },
        "labels": ["forge:managed", "forge:prd-pending"],
        "created": "2024-03-30T10:00:00.000+0000",
        "updated": "2024-03-30T10:05:00.000+0000",
        "creator": {"accountId": "user-123", "displayName": "Test User"},
        "reporter": {"accountId": "user-123", "displayName": "Test User"},
    },
}

# Sample Jira API GET /issue response for Bug
API_GET_ISSUE_BUG: dict[str, Any] = {
    "id": "10002",
    "key": "TEST-456",
    "self": "https://test.atlassian.net/rest/api/3/issue/10002",
    "fields": {
        "issuetype": {"name": "Bug", "id": "10002"},
        "project": {"key": "TEST", "name": "Test Project", "id": "10000"},
        "status": {"name": "New", "id": "1"},
        "summary": "Login fails with special characters in password",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "Steps to reproduce:\n1. Enter email\n2. Enter password with $@! characters\n3. Click login\n\nExpected: Login succeeds\nActual: 500 error\n\nStack trace:\nValueError: Invalid character in password field",
                        }
                    ],
                }
            ],
        },
        "labels": ["forge:managed"],
        "created": "2024-03-30T11:00:00.000+0000",
        "updated": "2024-03-30T11:00:00.000+0000",
    },
}


def make_jira_issue(
    key: str = "TEST-123",
    issue_type: str = "Feature",
    status: str = "New",
    summary: str = "Test Issue",
    description: str = "Test description",
    labels: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Factory function to create Jira issue API responses.

    Args:
        key: Jira issue key.
        issue_type: Issue type name (Feature, Bug, Epic, Task).
        status: Issue status name.
        summary: Issue summary.
        description: Issue description text.
        labels: List of labels.
        **kwargs: Additional field overrides.

    Returns:
        Jira API issue response dict.
    """
    if labels is None:
        labels = ["forge:managed"]

    issue = deepcopy(API_GET_ISSUE_FEATURE)
    issue["key"] = key
    issue["fields"]["issuetype"]["name"] = issue_type
    issue["fields"]["status"]["name"] = status
    issue["fields"]["summary"] = summary
    issue["fields"]["description"] = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": description}],
            }
        ],
    }
    issue["fields"]["labels"] = labels

    # Apply any additional overrides
    for field, value in kwargs.items():
        if field in issue["fields"]:
            issue["fields"][field] = value
        else:
            issue[field] = value

    return issue


def make_jira_webhook(
    event_type: str = "jira:issue_updated",
    issue_key: str = "TEST-123",
    issue_type: str = "Feature",
    labels: list[str] | None = None,
    changelog_field: str | None = None,
    changelog_from: str | None = None,
    changelog_to: str | None = None,
    comment_text: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Factory function to create Jira webhook payloads.

    Args:
        event_type: Webhook event type.
        issue_key: Jira issue key.
        issue_type: Issue type name.
        labels: List of labels on the issue.
        changelog_field: Field that changed (e.g., "labels", "status").
        changelog_from: Previous value.
        changelog_to: New value.
        comment_text: Comment body text (for comment events).
        **kwargs: Additional overrides.

    Returns:
        Jira webhook payload dict.
    """
    if labels is None:
        labels = ["forge:managed"]

    webhook = deepcopy(WEBHOOK_ISSUE_CREATED)
    webhook["webhookEvent"] = event_type
    webhook["issue"]["key"] = issue_key
    webhook["issue"]["fields"]["issuetype"]["name"] = issue_type
    webhook["issue"]["fields"]["labels"] = labels

    # Add changelog if specified
    if changelog_field:
        webhook["changelog"] = {
            "id": "12345",
            "items": [
                {
                    "field": changelog_field,
                    "fieldtype": "jira",
                    "from": None,
                    "fromString": changelog_from,
                    "to": None,
                    "toString": changelog_to,
                }
            ],
        }

    # Add comment if specified
    if comment_text:
        webhook["issue_event_type_name"] = "issue_commented"
        webhook["comment"] = {
            "id": "10100",
            "author": {
                "accountId": "user-456",
                "displayName": "PM User",
            },
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment_text}],
                    }
                ],
            },
            "created": "2024-03-30T10:10:00.000+0000",
        }

    # Apply additional overrides
    for field, value in kwargs.items():
        webhook[field] = value

    return webhook
