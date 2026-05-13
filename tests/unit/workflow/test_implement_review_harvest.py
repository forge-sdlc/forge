"""implement_review harvests review artifacts into state."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def review_state(tmp_path):
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "handoff.md").write_text("review fix done")
    return {
        "ticket_key": "TEST-1",
        "workspace_path": str(tmp_path),
        "current_repo": "org/repo",
        "context": {"branch_name": "forge/test-1", "guardrails": ""},
        "fork_owner": "fork-org",
        "fork_repo": "repo",
        "current_pr_number": 1,
        "current_pr_url": "https://github.com/org/repo/pull/1",
        "feedback_comment": "please fix the style",
        "revision_requested": True,
        "review_response_posted": False,
        "contested_comments": [],
        "is_paused": False,
        "retry_count": 0,
        "forge_artifacts": {},
        "spec_content": "",
        "implemented_tasks": [],
    }


@pytest.mark.asyncio
async def test_implement_review_harvests_plan_and_handoff(review_state, tmp_path):
    from forge.workflow.nodes.implement_review import implement_review

    forge_dir = tmp_path / ".forge"

    def phase1_side_effect(**kwargs):
        # Simulate Phase 1 container writing review-plan.md and review-objections.md
        (forge_dir / "review-plan.md").write_text("## Fix foo\n- change bar")
        return MagicMock(success=True)

    def phase2_side_effect(**kwargs):
        # Simulate Phase 2 container writing handoff.md
        (forge_dir / "handoff.md").write_text("review fix done")
        return MagicMock(success=True)

    mock_git = MagicMock()
    mock_git._run_git.return_value = MagicMock(stdout="abc123\n", returncode=0)
    mock_git.has_uncommitted_changes.return_value = False
    mock_git.push_to_fork = MagicMock()
    mock_git.add_fork_remote = MagicMock()

    call_count = 0

    async def mock_run(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return phase1_side_effect(**kwargs)
        else:
            return phase2_side_effect(**kwargs)

    mock_runner = MagicMock()
    mock_runner.run = mock_run

    mock_github = AsyncMock()

    with patch("forge.workflow.nodes.implement_review.prepare_workspace",
               return_value=(str(tmp_path), mock_git)), \
         patch("forge.workflow.nodes.implement_review.ContainerRunner", return_value=mock_runner), \
         patch("forge.workflow.nodes.implement_review._fetch_pr_review_comments",
               new_callable=AsyncMock, return_value="comment text"), \
         patch("forge.workflow.nodes.implement_review.run_post_change_review",
               new_callable=AsyncMock), \
         patch("forge.workflow.nodes.implement_review.sync_pr_description",
               new_callable=AsyncMock), \
         patch("forge.workflow.nodes.implement_review.GitHubClient", return_value=mock_github):
        result = await implement_review(review_state)

    artifacts = result.get("forge_artifacts", {}).get("org/repo", {})
    assert "review-plan.md" in artifacts
    assert "handoff.md" in artifacts
