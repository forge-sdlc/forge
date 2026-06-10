"""GitHub webhook and API response fixtures for testing."""

from copy import deepcopy
from typing import Any

# Sample GitHub webhook: check_run completed with success
WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS: dict[str, Any] = {
    "action": "completed",
    "check_run": {
        "id": 12345678,
        "name": "CI / Tests",
        "head_sha": "abc123def456",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2024-03-30T10:00:00Z",
        "completed_at": "2024-03-30T10:05:00Z",
        "output": {
            "title": "Tests passed",
            "summary": "All 42 tests passed",
        },
        "check_suite": {
            "id": 87654321,
            "head_branch": "feature/TEST-123",
        },
        "pull_requests": [
            {
                "id": 111,
                "number": 42,
                "url": "https://api.github.com/repos/org/repo/pulls/42",
                "head": {"ref": "feature/TEST-123"},
            }
        ],
    },
    "repository": {
        "id": 123456,
        "name": "repo",
        "full_name": "org/repo",
        "owner": {"login": "org"},
    },
    "sender": {"login": "github-actions[bot]"},
}

# Sample GitHub webhook: check_run completed with failure
WEBHOOK_CHECK_RUN_COMPLETED_FAILURE: dict[str, Any] = {
    "action": "completed",
    "check_run": {
        "id": 12345679,
        "name": "CI / Tests",
        "head_sha": "abc123def456",
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2024-03-30T10:00:00Z",
        "completed_at": "2024-03-30T10:03:00Z",
        "output": {
            "title": "Tests failed",
            "summary": "3 of 42 tests failed",
            "text": """
## Failed Tests

### test_login_validation
```
AssertionError: Expected status 200, got 400
  File "tests/test_auth.py", line 45
    assert response.status_code == 200
```

### test_password_special_chars
```
ValueError: Invalid character in password
  File "src/auth/validators.py", line 23
    raise ValueError("Invalid character in password")
```
""",
        },
        "check_suite": {
            "id": 87654321,
            "head_branch": "feature/TEST-123",
        },
        "pull_requests": [
            {
                "id": 111,
                "number": 42,
                "url": "https://api.github.com/repos/org/repo/pulls/42",
                "head": {"ref": "feature/TEST-123"},
            }
        ],
    },
    "repository": {
        "id": 123456,
        "name": "repo",
        "full_name": "org/repo",
        "owner": {"login": "org"},
    },
    "sender": {"login": "github-actions[bot]"},
}

# Sample GitHub webhook: pull_request review approved
WEBHOOK_PULL_REQUEST_REVIEW_APPROVED: dict[str, Any] = {
    "action": "submitted",
    "review": {
        "id": 999888777,
        "user": {"login": "reviewer", "id": 789},
        "body": "LGTM! Great work.",
        "state": "approved",
        "submitted_at": "2024-03-30T11:00:00Z",
        "commit_id": "abc123def456",
    },
    "pull_request": {
        "id": 111,
        "number": 42,
        "state": "open",
        "title": "TEST-123: Implement user authentication",
        "body": "## Summary\n- Added login endpoint\n- Added password validation",
        "head": {
            "ref": "feature/TEST-123",
            "sha": "abc123def456",
        },
        "base": {"ref": "main"},
        "html_url": "https://github.com/org/repo/pull/42",
        "user": {"login": "forge-bot"},
        "mergeable": True,
        "mergeable_state": "clean",
    },
    "repository": {
        "id": 123456,
        "name": "repo",
        "full_name": "org/repo",
        "owner": {"login": "org"},
    },
    "sender": {"login": "reviewer"},
}

# Sample GitHub API GET /pulls/{number} response
API_GET_PR: dict[str, Any] = {
    "id": 111,
    "number": 42,
    "state": "open",
    "title": "TEST-123: Implement user authentication",
    "body": "## Summary\n- Added login endpoint\n- Added password validation\n\n## Test Plan\n- [x] Unit tests added\n- [x] Integration tests pass",
    "head": {
        "ref": "feature/TEST-123",
        "sha": "abc123def456",
        "repo": {
            "name": "repo",
            "full_name": "org/repo",
        },
    },
    "base": {
        "ref": "main",
        "sha": "main123sha",
        "repo": {
            "name": "repo",
            "full_name": "org/repo",
        },
    },
    "html_url": "https://github.com/org/repo/pull/42",
    "url": "https://api.github.com/repos/org/repo/pulls/42",
    "user": {"login": "forge-bot", "id": 123},
    "created_at": "2024-03-30T10:00:00Z",
    "updated_at": "2024-03-30T10:30:00Z",
    "mergeable": True,
    "mergeable_state": "clean",
    "merged": False,
    "merge_commit_sha": None,
    "labels": [{"name": "forge:managed"}],
    "requested_reviewers": [],
    "draft": False,
}


def make_github_pr(
    number: int = 42,
    state: str = "open",
    title: str = "TEST-123: Test PR",
    head_ref: str = "feature/TEST-123",
    head_sha: str = "abc123def456",
    base_ref: str = "main",
    mergeable: bool = True,
    merged: bool = False,
    repo: str = "org/repo",
    **kwargs: Any,
) -> dict[str, Any]:
    """Factory function to create GitHub PR API responses.

    Args:
        number: PR number.
        state: PR state (open, closed, merged).
        title: PR title.
        head_ref: Head branch name.
        head_sha: Head commit SHA.
        base_ref: Base branch name.
        mergeable: Whether PR is mergeable.
        merged: Whether PR has been merged.
        repo: Repository full name (owner/repo).
        **kwargs: Additional field overrides.

    Returns:
        GitHub PR API response dict.
    """
    pr = deepcopy(API_GET_PR)
    pr["number"] = number
    pr["state"] = state
    pr["title"] = title
    pr["head"]["ref"] = head_ref
    pr["head"]["sha"] = head_sha
    pr["base"]["ref"] = base_ref
    pr["mergeable"] = mergeable
    pr["merged"] = merged
    pr["html_url"] = f"https://github.com/{repo}/pull/{number}"
    pr["url"] = f"https://api.github.com/repos/{repo}/pulls/{number}"

    owner, repo_name = repo.split("/")
    pr["head"]["repo"]["full_name"] = repo
    pr["head"]["repo"]["name"] = repo_name
    pr["base"]["repo"]["full_name"] = repo
    pr["base"]["repo"]["name"] = repo_name

    # Apply additional overrides
    for field, value in kwargs.items():
        pr[field] = value

    return pr


def make_check_run(
    name: str = "CI / Tests",
    conclusion: str = "success",
    head_sha: str = "abc123def456",
    pr_number: int = 42,
    repo: str = "org/repo",
    output_title: str | None = None,
    output_summary: str | None = None,
    output_text: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Factory function to create GitHub check_run webhook payloads.

    Args:
        name: Check run name.
        conclusion: Check run conclusion (success, failure, neutral, etc.).
        head_sha: Commit SHA.
        pr_number: Associated PR number.
        repo: Repository full name.
        output_title: Check run output title.
        output_summary: Check run output summary.
        output_text: Check run output text (detailed logs).
        **kwargs: Additional field overrides.

    Returns:
        GitHub check_run webhook payload dict.
    """
    template = (
        WEBHOOK_CHECK_RUN_COMPLETED_SUCCESS
        if conclusion == "success"
        else WEBHOOK_CHECK_RUN_COMPLETED_FAILURE
    )
    webhook = deepcopy(template)

    webhook["check_run"]["name"] = name
    webhook["check_run"]["conclusion"] = conclusion
    webhook["check_run"]["head_sha"] = head_sha
    webhook["check_run"]["pull_requests"][0]["number"] = pr_number

    owner, repo_name = repo.split("/")
    webhook["repository"]["full_name"] = repo
    webhook["repository"]["name"] = repo_name
    webhook["repository"]["owner"]["login"] = owner

    if output_title:
        webhook["check_run"]["output"]["title"] = output_title
    if output_summary:
        webhook["check_run"]["output"]["summary"] = output_summary
    if output_text:
        webhook["check_run"]["output"]["text"] = output_text

    # Apply additional overrides
    for field, value in kwargs.items():
        webhook[field] = value

    return webhook


def make_pull_request_review(
    action: str = "submitted",
    state: str = "approved",
    pr_number: int = 42,
    repo: str = "org/repo",
    reviewer: str = "reviewer",
    body: str = "LGTM!",
    **kwargs: Any,
) -> dict[str, Any]:
    """Factory function to create GitHub pull_request_review webhook payloads.

    Args:
        action: Review action (submitted, dismissed, edited).
        state: Review state (approved, changes_requested, commented).
        pr_number: PR number.
        repo: Repository full name.
        reviewer: Reviewer username.
        body: Review body text.
        **kwargs: Additional field overrides.

    Returns:
        GitHub pull_request_review webhook payload dict.
    """
    webhook = deepcopy(WEBHOOK_PULL_REQUEST_REVIEW_APPROVED)

    webhook["action"] = action
    webhook["review"]["state"] = state
    webhook["review"]["body"] = body
    webhook["review"]["user"]["login"] = reviewer
    webhook["pull_request"]["number"] = pr_number
    webhook["sender"]["login"] = reviewer

    owner, repo_name = repo.split("/")
    webhook["repository"]["full_name"] = repo
    webhook["repository"]["name"] = repo_name
    webhook["repository"]["owner"]["login"] = owner

    # Apply additional overrides
    for field, value in kwargs.items():
        webhook[field] = value

    return webhook
