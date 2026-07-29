"""Per-pull-request lifecycle state for multi-repository workflows."""

from copy import deepcopy
from typing import Any, TypedDict


class PullRequestState(TypedDict, total=False):
    url: str
    number: int | None
    repo: str
    fork_owner: str | None
    fork_repo: str | None
    ci_status: str | None
    ci_failed_checks: list[dict[str, Any]]
    ci_skipped_checks: list[str]
    ci_fix_attempt: int
    human_review_status: str | None
    review_comments: list[dict[str, Any]]
    contested_comments: list[dict[str, Any]]
    review_response_posted: bool
    merged: bool
    lifecycle_node: str


_ACTIVE_FIELDS = {
    "current_pr_url": "url",
    "current_pr_number": "number",
    "current_repo": "repo",
    "fork_owner": "fork_owner",
    "fork_repo": "fork_repo",
    "ci_status": "ci_status",
    "ci_failed_checks": "ci_failed_checks",
    "ci_skipped_checks": "ci_skipped_checks",
    "ci_fix_attempt": "ci_fix_attempt",
    "human_review_status": "human_review_status",
    "review_comments": "review_comments",
    "contested_comments": "contested_comments",
    "review_response_posted": "review_response_posted",
}

_PR_LIFECYCLE_NODES = {
    "wait_for_ci_gate",
    "ci_evaluator",
    "attempt_ci_fix",
    "human_review_gate",
    "implement_review",
    "review_response_gate",
    "rebase_pr",
}


def _event_pr_number(payload: dict[str, Any]) -> int | None:
    number = payload.get("pull_request", {}).get("number") or payload.get("issue", {}).get("number")
    if isinstance(number, int):
        return number
    for container in (payload.get("check_suite", {}), payload.get("check_run", {})):
        pull_requests = container.get("pull_requests", [])
        if pull_requests:
            number = pull_requests[0].get("number")
            return number if isinstance(number, int) else None
        suite_pull_requests = container.get("check_suite", {}).get("pull_requests", [])
        if suite_pull_requests:
            number = suite_pull_requests[0].get("number")
            return number if isinstance(number, int) else None
    return None


def save_active_pull_request(state: dict[str, Any]) -> dict[str, Any]:
    """Copy the scalar compatibility view into its per-repository PR record."""
    repo = state.get("current_repo")
    if not repo or state.get("current_pr_number") is None:
        return state

    pull_requests = deepcopy(state.get("pull_requests", {}))
    record = deepcopy(pull_requests.get(repo, {}))
    for scalar, per_pr in _ACTIVE_FIELDS.items():
        if scalar in state:
            record[per_pr] = deepcopy(state[scalar])
    record["repo"] = repo
    current_node = state.get("current_node")
    if current_node in _PR_LIFECYCLE_NODES:
        record["lifecycle_node"] = current_node
    else:
        record.setdefault("lifecycle_node", "wait_for_ci_gate")
    pull_requests[repo] = record
    return {**state, "pull_requests": pull_requests}


def activate_pull_request_for_event(
    state: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Select the PR targeted by a GitHub webhook as the scalar compatibility view."""
    repo = payload.get("repository", {}).get("full_name")
    number = _event_pr_number(payload)
    pull_requests = state.get("pull_requests", {})
    record = pull_requests.get(repo) if repo else None
    if not isinstance(record, dict) or (number is not None and record.get("number") != number):
        return state

    activated = {**state, "workspace_path": None}
    for scalar, per_pr in _ACTIVE_FIELDS.items():
        if per_pr in record:
            activated[scalar] = deepcopy(record[per_pr])
    if record.get("lifecycle_node") in _PR_LIFECYCLE_NODES:
        activated["current_node"] = record["lifecycle_node"]
    return activated


def all_pull_requests_merged(state: dict[str, Any]) -> bool:
    """Return true only when every created implementation PR has merged."""
    pull_requests = state.get("pull_requests", {})
    return bool(pull_requests) and all(
        record.get("merged", False) for record in pull_requests.values()
    )


def mark_active_pull_request_merged(state: dict[str, Any]) -> dict[str, Any]:
    """Mark the selected per-repository PR merged without changing other records."""
    repo = state.get("current_repo")
    pull_requests = deepcopy(state.get("pull_requests", {}))
    record = deepcopy(pull_requests.get(repo)) if repo else None
    if not isinstance(record, dict):
        return state
    record["merged"] = True
    pull_requests[repo] = record
    return {**state, "pull_requests": pull_requests}


def event_targets_pull_request(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Return whether a webhook identifies one of the implementation PR records."""
    repo = payload.get("repository", {}).get("full_name")
    number = _event_pr_number(payload)
    record = state.get("pull_requests", {}).get(repo)
    return isinstance(record, dict) and record.get("number") == number
