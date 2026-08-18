"""Unit tests for commit-message CI failure detection."""

from forge.workflow.utils.commit_message_ci import (
    commit_message_failure_summary,
    is_commit_message_formatting_failure,
)


def test_detects_check_commits_style_errors():
    checks = [
        {
            "name": "git-commits",
            "output": {
                "text": (
                    "./check-commits abc..def\n"
                    "error [1/2] title is longer than 72 characters, please make it shorter\n"
                    "error [1/2] title lacks a lowercase topic prefix (e.g. 'ipv6:')\n"
                    "error [1/2] 'Signed-off-by: Forge <forge@example.com>' trailer is missing\n"
                )
            },
        }
    ]
    assert is_commit_message_formatting_failure(checks) is True


def test_mixed_failures_are_not_commit_message_only():
    checks = [
        {"name": "git-commits", "output": {"text": "title lacks a lowercase topic prefix"}},
        {"name": "unit-tests", "output": {"text": "AssertionError: expected 1"}},
    ]
    assert is_commit_message_formatting_failure(checks) is False


def test_empty_checks_false():
    assert is_commit_message_formatting_failure([]) is False


def test_summary_mentions_amend():
    summary = commit_message_failure_summary(
        [{"name": "check-commits", "output": {"summary": "Signed-off-by trailer is missing"}}]
    )
    assert "amend the commit message" in summary
    assert "check-commits" in summary


def test_extract_amended_commit_message_from_plan():
    from forge.workflow.nodes.ci_evaluator import _extract_amended_commit_message

    plan = """# CI Fix Plan

## Fixable Failures

### git-commits
**Category**: commit-message

### Amended Commit Message
```
openflow: fix drain pending msgs

Signed-off-by: Forge <forge@example.com>
```
"""
    msg = _extract_amended_commit_message(plan)
    assert msg is not None
    assert msg.startswith("openflow: fix drain pending msgs")
    assert "Signed-off-by:" in msg
