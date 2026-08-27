"""Per-pull-request lifecycle state for multi-repository workflows."""

from copy import deepcopy
from typing import Any, TypedDict

from forge.integrations.source_control.contracts import NormalizedEvent


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
    pending_ci_event: bool


_ACTIVE_FIELDS = {
    "current_pr_url": "url",
    "current_pr_number": "number",
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
    "pending_ci_event": "pending_ci_event",
}

_PR_LIFECYCLE_NODES = {
    "ci_evaluator",
    "attempt_ci_fix",
    "human_review_gate",
    "implement_review",
    "review_response_gate",
    "rebase_pr",
}


def _numbered_key(repo: str, number: int | str) -> str:
    """Per-PR dict key from a repo namespace and a known PR number."""
    return f"{repo}:{number}"


def _url_key(repo: str, url: str) -> str:
    """Fallback dict key for a PR whose number is not yet known (see module docstring)."""
    return f"{repo}:{url}"


def _lookup_record(
    pull_requests: dict[str, Any], repo: str, number: int | str | None, url: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Find a PR record for ``repo``, preferring the per-PR numbered key and
    falling back to a URL-keyed record for a PR saved before its number was known.

    Also falls back to the legacy bare-``repo`` key from before per-PR keying
    was introduced, so a workflow checkpointed mid-CI/mid-review at deploy
    time doesn't get stranded — its record still lives under ``repo`` alone
    until the next ``save_active_pull_request`` migrates it to a per-PR key.

    Returns ``(key, record)`` or ``(None, None)`` when no record matches.
    """
    if number is not None:
        record = pull_requests.get(_numbered_key(repo, number))
        if isinstance(record, dict):
            return _numbered_key(repo, number), record
    if url:
        record = pull_requests.get(_url_key(repo, url))
        if isinstance(record, dict):
            return _url_key(repo, url), record
    record = pull_requests.get(repo)
    if isinstance(record, dict):
        return repo, record
    return None, None


def find_active_pull_request(
    pull_requests: dict[str, Any], repo: str, number: int | str | None, url: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Public wrapper around ``_lookup_record`` for callers outside this module
    (e.g. ``ci_evaluator``) that need the same numbered-key/URL-fallback lookup
    rather than duplicating the key-construction logic."""
    return _lookup_record(pull_requests, repo, number, url)


def _record_matches_event(record: dict[str, Any], event: NormalizedEvent) -> bool:
    if event.change_request is None:
        return False
    number = event.change_request.identity.native_id
    if number is None:
        return False
    if record.get("number") == number:
        return True
    return record.get("number") is None and event.change_request.url == record.get("url")


def save_active_pull_request(state: dict[str, Any]) -> dict[str, Any]:
    """Copy the scalar compatibility view into its per-PR record."""
    repo = state.get("current_repo")
    number = state.get("current_pr_number")
    url = state.get("current_pr_url")
    if not repo or (number is None and not url):
        return state

    key = _numbered_key(repo, number) if number is not None else _url_key(repo, url)
    existing_pull_requests = state.get("pull_requests", {})
    pull_requests = dict(existing_pull_requests)
    # Look up by number-or-url rather than `key` alone: a record saved before
    # the PR number was known lives under the url key, and once the number
    # becomes available `key` switches to the numbered key. Without this
    # lookup that stale url-keyed record would be missed and a fresh, empty
    # record created in its place, orphaning fields like lifecycle_node.
    existing_key, existing_record = _lookup_record(pull_requests, repo, number, url)
    record = deepcopy(existing_record) if existing_record is not None else {}
    if existing_key is not None and existing_key != key:
        del pull_requests[existing_key]
    for scalar, per_pr in _ACTIVE_FIELDS.items():
        if scalar in state:
            record[per_pr] = state[scalar]
    record["repo"] = repo
    current_node = state.get("current_node")
    if current_node in _PR_LIFECYCLE_NODES:
        record["lifecycle_node"] = current_node
    else:
        # Default to the post-PR entry node (teardown_workspace → human_review_gate)
        # so a stale record resolves to a real node on resume rather than a removed one.
        record.setdefault("lifecycle_node", "human_review_gate")
    pull_requests[key] = record
    return {**state, "pull_requests": pull_requests}


def activate_pull_request_for_event(
    state: dict[str, Any], event: NormalizedEvent | None
) -> dict[str, Any]:
    """Select the PR targeted by a source-control webhook as the scalar
    compatibility view. ``event`` is ``None`` for a Jira message (no PR to
    activate), in which case ``state`` is returned unchanged."""
    if event is None or event.change_request is None:
        return state

    repo = event.repo_ref.namespace
    number = event.change_request.identity.native_id
    url = event.change_request.url
    pull_requests = state.get("pull_requests", {})
    key, record = _lookup_record(pull_requests, repo, number, url)
    if record is None or not _record_matches_event(record, event):
        return state

    activated = {**state, "current_repo": repo}
    if record.get("number") is None:
        updated_pull_requests = deepcopy(pull_requests)
        record = updated_pull_requests.pop(key)
        record["number"] = number
        updated_pull_requests[_numbered_key(repo, number)] = record
        activated["pull_requests"] = updated_pull_requests
    if state.get("current_repo") != repo:
        activated["workspace_path"] = None
    for scalar, per_pr in _ACTIVE_FIELDS.items():
        if per_pr in record:
            activated[scalar] = deepcopy(record[per_pr])
    # The webhook is authoritative for the number. Set it after restoring the
    # compatibility fields so correctness does not depend on dict iteration order.
    activated["current_pr_number"] = number
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
    """Mark the selected per-PR record merged without changing other records."""
    repo = state.get("current_repo")
    if not repo:
        return state
    existing_pull_requests = state.get("pull_requests", {})
    key, record = _lookup_record(
        existing_pull_requests, repo, state.get("current_pr_number"), state.get("current_pr_url")
    )
    if record is None:
        return state
    pull_requests = dict(existing_pull_requests)
    record = deepcopy(record)
    record["merged"] = True
    pull_requests[key] = record
    return {**state, "pull_requests": pull_requests}


def event_targets_pull_request(state: dict[str, Any], event: NormalizedEvent | None) -> bool:
    """Return whether a webhook identifies one of the implementation PR records."""
    if event is None or event.change_request is None:
        return False
    _, record = _lookup_record(
        state.get("pull_requests", {}),
        event.repo_ref.namespace,
        event.change_request.identity.native_id,
        event.change_request.url,
    )
    return record is not None and _record_matches_event(record, event)
