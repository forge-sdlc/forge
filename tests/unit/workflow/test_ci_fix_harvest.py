"""attempt_ci_fix harvests fix-plan.md and handoff.md into state."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def ci_state(tmp_path):
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "fix-plan.md").write_text("fix plan content")
    (forge_dir / "handoff.md").write_text("ci fix handoff")
    (forge_dir / "ci-failures.md").write_text("failures")
    return {
        "ticket_key": "TEST-1",
        "workspace_path": str(tmp_path),
        "current_repo": "org/repo",
        "context": {"branch_name": "forge/test-1", "guardrails": ""},
        "fork_owner": "fork-org",
        "fork_repo": "repo",
        "ci_fix_attempts": 0,
        "ci_failed_checks": [{"name": "lint", "conclusion": "failure"}],
        "ci_skipped_checks": [],
        "pr_urls": ["https://github.com/org/repo/pull/1"],
        "current_pr_number": 1,
        "current_pr_url": "https://github.com/org/repo/pull/1",
        "is_paused": False,
        "retry_count": 0,
        "forge_artifacts": {},
        "spec_content": "",
    }


@pytest.mark.asyncio
async def test_attempt_ci_fix_harvests_fix_plan_and_handoff(ci_state, tmp_path):
    from forge.workflow.nodes.ci_evaluator import attempt_ci_fix

    mock_git = MagicMock()
    mock_git._run_git.return_value = MagicMock(stdout="abc123\n", returncode=0)
    mock_git.has_uncommitted_changes.return_value = False
    mock_git.push_to_fork = MagicMock()
    mock_git.add_fork_remote = MagicMock()

    mock_runner = AsyncMock()
    mock_runner.run.return_value = MagicMock(success=True)

    mock_github = AsyncMock()

    with patch("forge.workflow.nodes.ci_evaluator.prepare_workspace",
               return_value=(str(tmp_path), mock_git)), \
         patch("forge.workflow.nodes.ci_evaluator.ContainerRunner", return_value=mock_runner), \
         patch("forge.workflow.nodes.ci_evaluator.GitHubClient", return_value=mock_github), \
         patch("forge.workflow.nodes.ci_evaluator.Workspace", return_value=MagicMock()), \
         patch("forge.workflow.nodes.ci_evaluator.GitOperations", return_value=mock_git), \
         patch("forge.workflow.nodes.ci_evaluator.run_post_change_review",
               new=AsyncMock(return_value=(None, None))), \
         patch("forge.workflow.nodes.ci_evaluator.sync_pr_description",
               new_callable=AsyncMock), \
         patch("forge.workflow.nodes.ci_evaluator._fetch_ci_logs_and_artifacts",
               new_callable=AsyncMock), \
         patch("forge.workflow.nodes.ci_evaluator._collect_error_info", return_value="errors"):
        result = await attempt_ci_fix(ci_state)

    artifacts = result.get("forge_artifacts", {}).get("org/repo", {})
    assert artifacts.get("fix-plan.md") == "fix plan content"
    assert artifacts.get("handoff.md") == "ci fix handoff"
