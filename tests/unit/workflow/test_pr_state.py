from forge.workflow.pr_state import (
    activate_pull_request_for_event,
    all_pull_requests_merged,
    save_active_pull_request,
)


def _multi_repo_state() -> dict:
    return {
        "current_repo": "acme/frontend",
        "current_pr_number": 20,
        "current_pr_url": "https://github.com/acme/frontend/pull/20",
        "fork_owner": "forge-bot",
        "fork_repo": "frontend",
        "ci_status": "passed",
        "pr_merged": False,
        "pull_requests": {
            "acme/backend": {
                "repo": "acme/backend",
                "number": 10,
                "url": "https://github.com/acme/backend/pull/10",
                "fork_owner": "forge-bot",
                "fork_repo": "backend",
                "ci_status": "fixing",
                "ci_failed_checks": [{"name": "unit"}],
                "ci_fix_attempt": 2,
                "merged": False,
                "lifecycle_node": "review_response_gate",
            },
            "acme/frontend": {
                "repo": "acme/frontend",
                "number": 20,
                "url": "https://github.com/acme/frontend/pull/20",
                "fork_owner": "forge-bot",
                "fork_repo": "frontend",
                "ci_status": "passed",
                "merged": False,
                "lifecycle_node": "human_review_gate",
            },
        },
    }


def test_github_event_activates_matching_repo_pr() -> None:
    state = _multi_repo_state()
    payload = {
        "repository": {"full_name": "acme/backend"},
        "pull_request": {"number": 10},
    }

    activated = activate_pull_request_for_event(state, payload)

    assert activated["current_repo"] == "acme/backend"
    assert activated["current_pr_number"] == 10
    assert activated["fork_repo"] == "backend"
    assert activated["ci_status"] == "fixing"
    assert activated["ci_failed_checks"] == [{"name": "unit"}]
    assert activated["current_node"] == "review_response_gate"
    assert activated["workspace_path"] is None


def test_event_for_unknown_pr_does_not_change_active_repo() -> None:
    state = _multi_repo_state()
    payload = {
        "repository": {"full_name": "acme/other"},
        "pull_request": {"number": 99},
    }

    assert activate_pull_request_for_event(state, payload) == state


def test_issue_comment_event_activates_matching_repo_pr() -> None:
    state = _multi_repo_state()
    payload = {
        "repository": {"full_name": "acme/backend"},
        "issue": {"number": 10},
    }

    activated = activate_pull_request_for_event(state, payload)

    assert activated["current_repo"] == "acme/backend"
    assert activated["current_pr_number"] == 10


def test_active_changes_are_saved_only_to_matching_repo() -> None:
    state = activate_pull_request_for_event(
        _multi_repo_state(),
        {"repository": {"full_name": "acme/backend"}, "pull_request": {"number": 10}},
    )
    state["ci_status"] = "passed"
    state["ci_fix_attempt"] = 0

    saved = save_active_pull_request(state)

    assert saved["pull_requests"]["acme/backend"]["ci_status"] == "passed"
    assert saved["pull_requests"]["acme/backend"]["ci_fix_attempt"] == 0
    assert saved["pull_requests"]["acme/frontend"]["ci_status"] == "passed"


def test_merge_completion_requires_every_pr() -> None:
    state = _multi_repo_state()
    state["pull_requests"]["acme/backend"]["merged"] = True
    assert not all_pull_requests_merged(state)

    state["pull_requests"]["acme/frontend"]["merged"] = True
    assert all_pull_requests_merged(state)
