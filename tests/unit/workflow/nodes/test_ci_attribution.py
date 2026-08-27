"""Tests for CI attribution and evaluate_ci_status pending_ci_event clearing."""

from unittest.mock import AsyncMock, patch

import pytest

from forge.integrations.source_control.contracts import (
    ChangeRequest,
    ChangeRequestIdentity,
    ChangeRequestState,
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Provider,
    RepositoryRef,
)


def _repo_ref(repo: str = "org/repo") -> RepositoryRef:
    return RepositoryRef(
        id=repo,
        provider=Provider.GITHUB,
        connection="c",
        namespace=repo,
        default_branch="main",
        change_request_mode="fork",
    )


def _mock_adapter(checks, pr_number=1, repo="org/repo"):
    """Patch target for ci_evaluator.get_adapter returning the given checks."""
    adapter = AsyncMock()
    adapter.get_change_request = AsyncMock(
        return_value=ChangeRequest(
            identity=ChangeRequestIdentity(connection="c", repository_id=repo, native_id=pr_number),
            url=f"https://github.com/{repo}/pull/{pr_number}",
            title="t",
            body="",
            state=ChangeRequestState.OPEN,
            source_branch="feature",
            target_branch="main",
        )
    )
    adapter.get_checks = AsyncMock(return_value=checks)
    return adapter


BASE_STATE = {
    "ticket_key": "TEST-1",
    "current_repo": "org/repo",
    "current_pr_number": 1,
    "pr_urls": ["https://github.com/org/repo/pull/1"],
    "ci_fix_attempt": 0,
    "ci_fix_max_attempts": 5,
    "pending_ci_event": True,
    "ci_skipped_checks": [],
}

ATTEMPT_BASE_STATE = {
    "ticket_key": "TEST-1",
    "ci_failed_checks": [
        {"name": "unit-tests", "conclusion": "failure", "log_url": "", "output": {}}
    ],
    "ci_fix_attempt": 1,
    "ci_fix_max_attempts": 5,
    "current_node": "attempt_ci_fix",
    "current_repo": "org/repo",
    "context": {"branch_name": "forge/TEST-1"},
    "pending_ci_event": False,
}


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
async def test_evaluate_ci_status_clears_pending_ci_event_on_pending(mock_get_adapter):
    """evaluate_ci_status returns pending_ci_event=False when CI is still running."""
    from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

    mock_get_adapter.return_value = (_repo_ref(), _mock_adapter([]))  # No checks yet = pending

    state = {**BASE_STATE, "pending_ci_event": True}
    result = await evaluate_ci_status(state)

    assert result.get("ci_status") == "pending"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
async def test_evaluate_ci_status_clears_pending_ci_event_on_passed(MockJira, mock_get_adapter):
    """evaluate_ci_status returns pending_ci_event=False when all CI passes."""
    from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

    mock_get_adapter.return_value = (
        _repo_ref(),
        _mock_adapter(
            [
                CheckRun(
                    name="unit-tests",
                    status=CheckStatus.COMPLETED,
                    conclusion=CheckConclusion.SUCCESS,
                )
            ]
        ),
    )

    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()

    state = {**BASE_STATE, "pending_ci_event": True}
    result = await evaluate_ci_status(state)

    assert result.get("ci_status") == "passed"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
async def test_evaluate_ci_status_finds_url_keyed_pr_when_number_unknown(mock_get_adapter):
    """A PR saved before its number was known is stored under a URL key
    (see pr_state.py). evaluate_ci_status must locate it via that fallback
    instead of failing with "Active pull request state is inconsistent"."""
    from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

    mock_get_adapter.return_value = (_repo_ref(), _mock_adapter([]))  # No checks yet = pending

    pr_url = "https://github.com/org/repo/pull/1"
    state = {
        **BASE_STATE,
        "current_repo": "org/repo",
        "current_pr_url": pr_url,
        "current_pr_number": None,
        "pull_requests": {
            f"org/repo:{pr_url}": {"url": pr_url, "number": None, "repo": "org/repo"}
        },
        "pending_ci_event": True,
    }

    result = await evaluate_ci_status(state)

    assert result.get("ci_status") == "pending"
    assert result.get("last_error") is None


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_external_skips_fix(
    _mock_fetch, mock_get_adapter, MockJira, mock_prep, MockRunner, tmp_path
):
    """Phase 0 verdict attributable=false → external_failure, no fix attempt increment."""
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    # Phase 0 container writes attribution file
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    attribution_file = forge_dir / "ci-attribution.json"

    mock_prep.return_value = (str(tmp_path), None)

    mock_runner = AsyncMock()
    MockRunner.return_value = mock_runner

    async def write_attribution(**_kwargs):
        attribution_file.write_text(
            '{"attributable": false, "reason": "Flaky infra", "confidence": "high"}'
        )

    mock_runner.run = AsyncMock(side_effect=write_attribution)

    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()

    mock_get_adapter.return_value = (_repo_ref(), AsyncMock())

    state = {**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)}
    result = await attempt_ci_fix(state)

    assert result["ci_status"] == "external_failure"
    assert result["ci_fix_attempt"] == 0  # Reserved attempt refunded
    assert result["current_node"] == "human_review_gate"
    assert result.get("pending_ci_event", True) is False


def test_attribution_prompt_compares_full_pr_against_default_branch():
    """Attribution must inspect the whole PR, not only its latest commit."""
    from forge.prompts import load_prompt

    prompt = load_prompt(
        "ci-attribution",
        failures_file_path=".forge/ci-failures.md",
        base_branch="develop",
    )

    assert "git merge-base HEAD origin/develop" in prompt
    assert "HEAD~1 HEAD" not in prompt


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_attributable_proceeds_to_fix(
    _mock_fetch, mock_get_adapter, MockJira, mock_prep, MockRunner, tmp_path
):
    """Phase 0 verdict attributable=true → proceeds to fix, increments attempt."""
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    attribution_file = forge_dir / "ci-attribution.json"
    attribution_file.write_text(
        '{"attributable": true, "reason": "auth.py modified", "confidence": "high"}'
    )
    # Phase 1 writes no fix plan → short-circuit back to gate
    # (fix_plan_file does not exist)

    mock_prep.return_value = (str(tmp_path), None)

    mock_runner = AsyncMock()
    MockRunner.return_value = mock_runner
    mock_runner.run = AsyncMock()

    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()

    mock_get_adapter.return_value = (_repo_ref(), AsyncMock())

    state = {**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)}
    result = await attempt_ci_fix(state)

    # Proceeds: returns to gate, pending_ci_event cleared
    assert result["current_node"] == "human_review_gate"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
async def test_attempt_ci_fix_with_no_failed_checks_reverifies_instead_of_asserting_pass():
    """attempt_ci_fix should only ever be routed to with ci_failed_checks
    populated. If it's unexpectedly empty (e.g. a concurrent/stale state
    update cleared it), the node must re-verify live CI via ci_evaluator
    rather than asserting ci_status=passed without checking."""
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    state = {**ATTEMPT_BASE_STATE, "ci_failed_checks": []}
    result = await attempt_ci_fix(state)

    assert result["current_node"] == "ci_evaluator"
    assert result.get("ci_status") != "passed"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_missing_file_proceeds_as_attributable(
    _mock_fetch, mock_get_adapter, MockJira, mock_prep, MockRunner, tmp_path
):
    """Missing ci-attribution.json is treated as attributable (fail-safe)."""
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    # No attribution file written — container failed silently

    mock_prep.return_value = (str(tmp_path), None)
    mock_runner = AsyncMock()
    MockRunner.return_value = mock_runner
    mock_runner.run = AsyncMock()
    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()
    mock_get_adapter.return_value = (_repo_ref(), AsyncMock())

    state = {**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)}
    result = await attempt_ci_fix(state)

    # Fail-safe: assumes attributable, proceeds
    assert result.get("ci_status") != "external_failure"
    assert result["current_node"] == "human_review_gate"


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.get_adapter")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_does_not_reuse_stale_external_verdict(
    _mock_fetch, mock_get_adapter, MockJira, mock_prep, MockRunner, tmp_path
):
    """A missing fresh verdict must not reuse an external verdict from an earlier CI cycle."""
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    attribution_file = forge_dir / "ci-attribution.json"
    attribution_file.write_text(
        '{"attributable": false, "reason": "Previous infrastructure failure"}'
    )

    mock_prep.return_value = (str(tmp_path), None)
    mock_runner = AsyncMock()
    MockRunner.return_value = mock_runner
    mock_runner.run = AsyncMock()  # Simulate a run that writes no fresh verdict.
    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()
    mock_get_adapter.return_value = (_repo_ref(), AsyncMock())

    result = await attempt_ci_fix({**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)})

    assert result.get("ci_status") != "external_failure"
    assert result["ci_fix_attempt"] == 1
    assert not attribution_file.exists()


def test_wait_for_ci_gate_does_not_exist():
    """wait_for_ci_gate has been deleted from ci_evaluator module."""
    import forge.workflow.nodes.ci_evaluator as mod

    assert not hasattr(mod, "wait_for_ci_gate"), (
        "wait_for_ci_gate must be deleted — it no longer exists as a graph node"
    )
