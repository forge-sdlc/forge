"""Tests for CI attribution and evaluate_ci_status pending_ci_event clearing."""

from unittest.mock import AsyncMock, patch

import pytest

BASE_STATE = {
    "ticket_key": "TEST-1",
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
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
async def test_evaluate_ci_status_clears_pending_ci_event_on_pending(MockGitHub):
    """evaluate_ci_status returns pending_ci_event=False when CI is still running."""
    from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github
    mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc123"}})
    mock_github.get_check_runs = AsyncMock(return_value=[])  # No checks yet = pending
    mock_github.close = AsyncMock()

    state = {**BASE_STATE, "pending_ci_event": True}
    result = await evaluate_ci_status(state)

    assert result.get("ci_status") == "pending"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
async def test_evaluate_ci_status_clears_pending_ci_event_on_passed(MockJira, MockGitHub):
    """evaluate_ci_status returns pending_ci_event=False when all CI passes."""
    from forge.workflow.nodes.ci_evaluator import evaluate_ci_status

    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github
    mock_github.get_pull_request = AsyncMock(return_value={"head": {"sha": "abc123"}})
    mock_github.get_check_runs = AsyncMock(
        return_value=[{"name": "unit-tests", "status": "completed", "conclusion": "success"}]
    )
    mock_github.close = AsyncMock()

    mock_jira = AsyncMock()
    MockJira.return_value = mock_jira
    mock_jira.close = AsyncMock()

    state = {**BASE_STATE, "pending_ci_event": True}
    result = await evaluate_ci_status(state)

    assert result.get("ci_status") == "passed"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_external_skips_fix(
    _mock_fetch, MockGitHub, MockJira, mock_prep, MockRunner, tmp_path
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

    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github

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
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_attributable_proceeds_to_fix(
    _mock_fetch, MockGitHub, MockJira, mock_prep, MockRunner, tmp_path
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

    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github

    state = {**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)}
    result = await attempt_ci_fix(state)

    # Proceeds: returns to gate, pending_ci_event cleared
    assert result["current_node"] == "human_review_gate"
    assert result.get("pending_ci_event", True) is False


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_missing_file_proceeds_as_attributable(
    _mock_fetch, MockGitHub, MockJira, mock_prep, MockRunner, tmp_path
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
    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github

    state = {**ATTEMPT_BASE_STATE, "workspace_path": str(tmp_path)}
    result = await attempt_ci_fix(state)

    # Fail-safe: assumes attributable, proceeds
    assert result.get("ci_status") != "external_failure"
    assert result["current_node"] == "human_review_gate"


@pytest.mark.asyncio
@patch("forge.workflow.nodes.ci_evaluator.ContainerRunner")
@patch("forge.workflow.nodes.ci_evaluator.prepare_workspace")
@patch("forge.workflow.nodes.ci_evaluator.JiraClient")
@patch("forge.workflow.nodes.ci_evaluator.GitHubClient")
@patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts", new_callable=AsyncMock)
async def test_attribution_does_not_reuse_stale_external_verdict(
    _mock_fetch, MockGitHub, MockJira, mock_prep, MockRunner, tmp_path
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
    mock_github = AsyncMock()
    MockGitHub.return_value = mock_github

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
