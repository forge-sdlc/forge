from datetime import UTC, datetime

from forge.integrations.source_control.contracts import (
    Actor,
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    EventKind,
    NormalizedEvent,
    Provider,
    RepositoryRef,
)
from forge.workflow.pr_state import (
    activate_pull_request_for_event,
    all_pull_requests_merged,
    event_targets_pull_request,
    save_active_pull_request,
)


def _event(
    repo="acme/payments",
    native_id=42,
    url="https://github.com/acme/payments/pull/42",
) -> NormalizedEvent:
    repo_ref = RepositoryRef(
        id=repo,
        provider=Provider.GITHUB,
        connection="default-github",
        namespace=repo,
        default_branch="main",
        change_request_mode="fork",
    )
    return NormalizedEvent(
        id="e1",
        kind=EventKind.CR_UPDATED,
        repo_ref=repo_ref,
        actor=Actor(login="octocat", is_bot=False),
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        change_request=ChangeRequest(
            identity=ChangeRequestIdentity(
                connection="default-github", repository_id=repo, native_id=native_id
            ),
            url=url,
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
            draft=False,
        ),
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
            "acme/backend:10": {
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
            "acme/frontend:20": {
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


# ── Brief Step 1: core NormalizedEvent keying ──────────────────────────────


def test_activate_pull_request_for_event_keys_by_repo_and_number() -> None:
    event = _event()
    key = "acme/payments:42"
    state = {"pull_requests": {key: {"number": 42, "url": event.change_request.url}}}

    activated = activate_pull_request_for_event(state, event)

    assert activated["current_pr_number"] == 42
    assert activated["current_repo"] == "acme/payments"


def test_activate_pull_request_for_event_returns_state_unchanged_for_none_event() -> None:
    state = {"pull_requests": {}}

    assert activate_pull_request_for_event(state, None) is state


def test_event_targets_pull_request_true_for_matching_key() -> None:
    event = _event()
    key = "acme/payments:42"
    state = {"pull_requests": {key: {"number": 42, "url": event.change_request.url}}}

    assert event_targets_pull_request(state, event) is True


def test_event_targets_pull_request_false_for_none_event() -> None:
    assert event_targets_pull_request({"pull_requests": {}}, None) is False


def test_two_prs_same_repo_get_distinct_keys() -> None:
    """The whole point of keying by repo+number instead of owner/repo: multiple
    concurrent PRs against the same repo must not collide on one dict slot."""
    state = {
        "pull_requests": {
            "acme/payments:42": {
                "number": 42,
                "url": "https://github.com/acme/payments/pull/42",
                "ci_status": "passed",
            },
            "acme/payments:43": {
                "number": 43,
                "url": "https://github.com/acme/payments/pull/43",
                "ci_status": "fixing",
            },
        }
    }
    event_42 = _event(native_id=42, url="https://github.com/acme/payments/pull/42")
    event_43 = _event(native_id=43, url="https://github.com/acme/payments/pull/43")

    activated_42 = activate_pull_request_for_event(state, event_42)
    activated_43 = activate_pull_request_for_event(state, event_43)

    assert activated_42["current_pr_number"] == 42
    assert activated_42["ci_status"] == "passed"
    assert activated_43["current_pr_number"] == 43
    assert activated_43["ci_status"] == "fixing"


# ── Preserved behavioral coverage (ported from the raw-payload version) ─────


def test_event_activates_matching_repo_pr() -> None:
    state = _multi_repo_state()
    event = _event(repo="acme/backend", native_id=10)

    activated = activate_pull_request_for_event(state, event)

    assert activated["current_repo"] == "acme/backend"
    assert activated["current_pr_number"] == 10
    assert activated["fork_repo"] == "backend"
    assert activated["ci_status"] == "fixing"
    assert activated["ci_failed_checks"] == [{"name": "unit"}]
    assert activated["current_node"] == "review_response_gate"
    assert activated["workspace_path"] is None


def test_event_for_unknown_pr_does_not_change_active_repo() -> None:
    state = _multi_repo_state()
    event = _event(repo="acme/other", native_id=99)

    assert activate_pull_request_for_event(state, event) == state


def test_same_repo_event_preserves_existing_workspace() -> None:
    state = _multi_repo_state()
    state["workspace_path"] = "/tmp/forge-AISOS-1-active"
    event = _event(repo="acme/frontend", native_id=20)

    activated = activate_pull_request_for_event(state, event)

    assert activated["workspace_path"] == "/tmp/forge-AISOS-1-active"


def test_active_changes_are_saved_only_to_matching_repo() -> None:
    state = activate_pull_request_for_event(
        _multi_repo_state(),
        _event(repo="acme/backend", native_id=10),
    )
    state["ci_status"] = "passed"
    state["ci_fix_attempt"] = 0

    saved = save_active_pull_request(state)

    assert saved["pull_requests"]["acme/backend:10"]["ci_status"] == "passed"
    assert saved["pull_requests"]["acme/backend:10"]["ci_fix_attempt"] == 0
    # The frontend record is left untouched.
    assert saved["pull_requests"]["acme/frontend:20"]["ci_status"] == "passed"


def test_save_creates_distinct_slot_per_pr_number() -> None:
    """Two PRs on the same repo saved from the scalar view land in separate slots."""
    base = _multi_repo_state()
    base["pull_requests"] = {}

    first = save_active_pull_request(
        {
            **base,
            "current_repo": "acme/api",
            "current_pr_number": 1,
            "current_pr_url": "https://github.com/acme/api/pull/1",
            "ci_status": "passed",
        }
    )
    both = save_active_pull_request(
        {
            **first,
            "current_repo": "acme/api",
            "current_pr_number": 2,
            "current_pr_url": "https://github.com/acme/api/pull/2",
            "ci_status": "fixing",
        }
    )

    assert both["pull_requests"]["acme/api:1"]["ci_status"] == "passed"
    assert both["pull_requests"]["acme/api:2"]["ci_status"] == "fixing"


def test_merge_completion_requires_every_pr() -> None:
    state = _multi_repo_state()
    state["pull_requests"]["acme/backend:10"]["merged"] = True
    assert not all_pull_requests_merged(state)

    state["pull_requests"]["acme/frontend:20"]["merged"] = True
    assert all_pull_requests_merged(state)


# ── Number-unknown (URL-keyed) fallback ────────────────────────────────────


def test_url_only_pr_blocks_aggregate_merge_completion() -> None:
    state = _multi_repo_state()
    state["pull_requests"]["acme/backend:10"]["merged"] = True
    state["pull_requests"]["acme/frontend:20"]["merged"] = True
    state["pull_requests"]["acme/docs:https://github.com/acme/docs/pull/30"] = {
        "repo": "acme/docs",
        "url": "https://github.com/acme/docs/pull/30",
        "number": None,
        "merged": False,
    }

    assert not all_pull_requests_merged(state)


def test_save_with_unknown_number_keys_by_url() -> None:
    saved = save_active_pull_request(
        {
            "current_repo": "acme/docs",
            "current_pr_number": None,
            "current_pr_url": "https://github.com/acme/docs/pull/30",
            "pull_requests": {},
        }
    )

    assert "acme/docs:https://github.com/acme/docs/pull/30" in saved["pull_requests"]
    assert saved["pull_requests"]["acme/docs:https://github.com/acme/docs/pull/30"]["number"] is None


def test_save_without_number_or_url_is_noop() -> None:
    state = {"current_repo": "acme/docs", "pull_requests": {}}
    assert save_active_pull_request(state) is state


def test_later_webhook_hydrates_and_rekeys_url_only_pr() -> None:
    url = "https://github.com/acme/docs/pull/30"
    state = _multi_repo_state()
    state["pull_requests"][f"acme/docs:{url}"] = {
        "repo": "acme/docs",
        "url": url,
        "number": None,
        "merged": False,
    }
    event = _event(repo="acme/docs", native_id=30, url=url)

    activated = activate_pull_request_for_event(state, event)

    assert activated["current_repo"] == "acme/docs"
    assert activated["current_pr_number"] == 30
    # Record re-keyed from its URL slot to the numbered slot; no duplicate left.
    assert "acme/docs:30" in activated["pull_requests"]
    assert f"acme/docs:{url}" not in activated["pull_requests"]
    assert activated["pull_requests"]["acme/docs:30"]["number"] == 30


def test_rekeyed_pr_does_not_duplicate_on_subsequent_save() -> None:
    url = "https://github.com/acme/docs/pull/30"
    state = _multi_repo_state()
    state["pull_requests"][f"acme/docs:{url}"] = {
        "repo": "acme/docs",
        "url": url,
        "number": None,
        "merged": False,
    }
    activated = activate_pull_request_for_event(
        state, _event(repo="acme/docs", native_id=30, url=url)
    )
    activated["ci_status"] = "passed"

    saved = save_active_pull_request(activated)

    docs_keys = [k for k in saved["pull_requests"] if k.startswith("acme/docs:")]
    assert docs_keys == ["acme/docs:30"]
    assert saved["pull_requests"]["acme/docs:30"]["ci_status"] == "passed"


def test_url_only_pr_rejects_different_pr_in_same_repo() -> None:
    url = "https://github.com/acme/docs/pull/30"
    state = _multi_repo_state()
    state["pull_requests"][f"acme/docs:{url}"] = {
        "repo": "acme/docs",
        "url": url,
        "number": None,
        "merged": False,
    }
    # A different PR (#31) on the same repo must not match the url-only #30 record.
    event = _event(
        repo="acme/docs",
        native_id=31,
        url="https://github.com/acme/docs/pull/31",
    )

    assert not event_targets_pull_request(state, event)


# ── Legacy bare-repo key fallback ───────────────────────────────────────────
# Workflows checkpointed mid-CI/mid-review before per-PR keying shipped have
# their record stored under `repo` alone rather than `repo:number`/`repo:url`.


def _legacy_state() -> dict:
    return {
        "current_repo": None,
        "current_pr_number": None,
        "current_pr_url": None,
        "pull_requests": {
            "acme/legacy": {
                "repo": "acme/legacy",
                "number": 99,
                "url": "https://github.com/acme/legacy/pull/99",
                "ci_status": "pending",
                "merged": False,
                "lifecycle_node": "ci_evaluator",
            },
        },
    }


def test_event_targets_pull_request_matches_legacy_bare_repo_key() -> None:
    state = _legacy_state()
    event = _event(
        repo="acme/legacy", native_id=99, url="https://github.com/acme/legacy/pull/99"
    )

    assert event_targets_pull_request(state, event)


def test_activate_pull_request_for_event_hydrates_from_legacy_bare_repo_key() -> None:
    state = _legacy_state()
    event = _event(
        repo="acme/legacy", native_id=99, url="https://github.com/acme/legacy/pull/99"
    )

    activated = activate_pull_request_for_event(state, event)

    assert activated["current_repo"] == "acme/legacy"
    assert activated["current_pr_number"] == 99
    assert activated["ci_status"] == "pending"


def test_save_migrates_legacy_bare_repo_key_to_numbered_key() -> None:
    state = _legacy_state()
    event = _event(
        repo="acme/legacy", native_id=99, url="https://github.com/acme/legacy/pull/99"
    )
    activated = activate_pull_request_for_event(state, event)
    activated["ci_status"] = "passed"

    saved = save_active_pull_request(activated)

    assert "acme/legacy" not in saved["pull_requests"]
    assert saved["pull_requests"]["acme/legacy:99"]["ci_status"] == "passed"
    assert saved["pull_requests"]["acme/legacy:99"]["lifecycle_node"] == "ci_evaluator"
