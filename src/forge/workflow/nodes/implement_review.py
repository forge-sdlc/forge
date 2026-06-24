"""implement_review node — addresses PR review feedback on an existing branch."""

import logging
from pathlib import Path
from typing import Any

from langgraph.graph import END

from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.code_review import run_post_change_review, sync_pr_description
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.utils import set_paused, update_state_timestamp

logger = logging.getLogger(__name__)

_REVIEW_COMMENTS_FILE = ".forge/review-comments.md"
_REVIEW_PLAN_FILE = ".forge/review-plan.md"
_REVIEW_OBJECTIONS_FILE = ".forge/review-objections.md"


def review_response_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow awaiting human confirmation of contested review comments."""
    ticket_key = state["ticket_key"]
    logger.info(f"Review response gate: pausing for {ticket_key}")
    return set_paused(state, "review_response_gate")


def route_review_response(state: WorkflowState) -> str:
    """Route after human responds to the agent's review objection."""
    if state.get("is_paused"):
        return END

    revision_requested = state.get("revision_requested", False)
    contested_comments = state.get("contested_comments", [])

    # Confirmed: revision still requested but contested_comments cleared by worker
    if revision_requested and not contested_comments:
        return "implement_review"

    return "human_review_gate"


async def _fetch_pr_review_comments(owner: str, repo: str, pr_number: int, review_body: str) -> str:
    """Fetch all PR review comments and format them for the analysis container.

    Combines the review summary body with all inline review comments so the
    analysis agent has the full picture.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: PR number.
        review_body: The review summary body from the webhook.

    Returns:
        Formatted markdown string of all review feedback.
    """
    github = GitHubClient()
    try:
        inline_comments = await github.get_pull_request_review_comments(owner, repo, pr_number)
    except Exception as e:
        logger.warning(f"Could not fetch inline review comments: {e}")
        inline_comments = []
    finally:
        await github.close()

    lines = ["# PR Review Feedback\n"]

    if review_body and review_body.strip():
        lines.append("## Review Summary\n")
        lines.append(review_body.strip())
        lines.append("\n")

    if inline_comments:
        lines.append("## Inline Comments\n")
        for comment in inline_comments:
            path = comment.get("path", "")
            position = comment.get("original_position") or comment.get("position", "")
            body = comment.get("body", "")
            lines.append(f"### `{path}` (line {position})\n")
            lines.append(body.strip())
            lines.append("\n")

    return "\n".join(lines)


async def implement_review(state: WorkflowState) -> WorkflowState:
    """Implement PR review feedback on the existing branch.

    Two-phase approach:
    1. Analysis container: reads all PR review comments from .forge/review-comments.md,
       explores the code using git tools, outputs .forge/review-plan.md (actionable items)
       and .forge/review-objections.md (contested items). Makes NO code changes.
    2. If objections exist: post to PR/Jira, pause at review_response_gate.
    3. Implementation container: reads .forge/review-plan.md, makes targeted changes,
       commits. Only runs when review-plan.md has actionable items.
    4. Post-change review + push (only if commits were made).

    Args:
        state: Current workflow state.

    Returns:
        Updated state after review feedback is addressed.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")
    feedback_comment = state.get("feedback_comment", "")
    fork_owner = state.get("fork_owner", "")
    fork_repo = state.get("fork_repo", "")
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")
    pr_number = state.get("current_pr_number")

    logger.info(f"Implementing PR review feedback for {ticket_key}")

    settings = get_settings()

    try:
        try:
            workspace_path, git = prepare_workspace(state)
            state = {**state, "workspace_path": workspace_path}
        except ValueError as e:
            return update_state_timestamp(
                {
                    **state,
                    "last_error": str(e),
                    "current_node": "implement_review",
                    "retry_count": state.get("retry_count", 0) + 1,
                }
            )

        # ── Phase 0: Fetch all PR review comments from GitHub ─────────────────
        _owner, _, _repo = current_repo.partition("/")
        review_comments_text = await _fetch_pr_review_comments(
            owner=_owner,
            repo=_repo,
            pr_number=pr_number or 0,
            review_body=feedback_comment,
        )

        # Write all review comments to a file so the container can read them
        forge_dir = Path(workspace_path) / ".forge"
        forge_dir.mkdir(parents=True, exist_ok=True)
        (forge_dir / "review-comments.md").write_text(review_comments_text)

        # Clear previous analysis files
        for fname in (_REVIEW_PLAN_FILE, _REVIEW_OBJECTIONS_FILE):
            fpath = Path(workspace_path) / fname
            if fpath.exists():
                fpath.unlink()

        # ── Phase 1: Analysis container ───────────────────────────────────────
        # The container reads .forge/review-comments.md, explores the code,
        # and outputs .forge/review-plan.md + .forge/review-objections.md.
        # It makes NO code changes.
        analysis_prompt = load_prompt("implement-review", ticket_key=ticket_key)

        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Analyze PR review feedback for {ticket_key}",
            task_description=analysis_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-review-analyze",
            repo_name=current_repo,
        )

        # ── Check for objections ──────────────────────────────────────────────
        objections_path = Path(workspace_path) / _REVIEW_OBJECTIONS_FILE
        if objections_path.exists():
            objections_text = objections_path.read_text().strip()
            if objections_text:
                logger.info(f"Agent contested review comments for {ticket_key}")
                await _post_review_objection(
                    state=state,
                    objections=objections_text,
                    owner=_owner,
                    repo=_repo,
                    pr_number=pr_number,
                )
                return update_state_timestamp(
                    {
                        **state,
                        "review_response_posted": True,
                        "contested_comments": [{"text": objections_text}],
                        "current_node": "review_response_gate",
                    }
                )

        # ── Phase 2: Implementation container ────────────────────────────────
        # Only runs if the analysis produced actionable items.
        plan_path = Path(workspace_path) / _REVIEW_PLAN_FILE
        plan_text = plan_path.read_text().strip() if plan_path.exists() else ""

        if not plan_text or plan_text == "# No actionable items":
            logger.info(f"No actionable review items for {ticket_key} — nothing to implement")
        else:
            fix_prompt = load_prompt("implement-review-fix", ticket_key=ticket_key)

            runner = ContainerRunner(settings)
            await runner.run(
                workspace_path=Path(workspace_path),
                task_summary=f"Implement PR review plan for {ticket_key}",
                task_description=fix_prompt,
                ticket_key=ticket_key,
                task_key=f"{ticket_key}-review-fix",
                repo_name=current_repo,
            )

            # Commit any uncommitted changes the container left
            if git.has_uncommitted_changes():
                git.stage_all()
                git.commit(f"[{ticket_key}] review: address PR feedback")

        # ── Push only if there are new commits ───────────────────────────────
        if fork_owner and fork_repo:
            git.add_fork_remote(fork_owner, fork_repo)
            remote_ref = f"fork/{branch_name}"
        else:
            remote_ref = f"origin/{branch_name}"

        unpushed = git._run_git(
            "log", f"{remote_ref}..HEAD", "--oneline", check=False
        ).stdout.strip()

        if unpushed:
            # Run post-change review before pushing (only when there are commits)
            await run_post_change_review(
                workspace_path=workspace_path,
                ticket_key=ticket_key,
                current_repo=current_repo,
                branch_name=branch_name,
                spec_content=state.get("spec_content", ""),
                guardrails=state.get("context", {}).get("guardrails", ""),
                label="review-impl",
            )

            if fork_owner and fork_repo:
                git.push_to_fork(force=False)
            else:
                git.push(force=False)
            logger.info(f"Review implementation pushed for {ticket_key}")

            await sync_pr_description(
                state,
                git,
                owner=_owner,
                repo=_repo,
                pr_number=pr_number,
                attempt=0,
            )
        else:
            logger.info(f"No new commits after review implementation for {ticket_key}")

        # Only re-enter the CI gate if we actually pushed new commits; otherwise
        # CI won't re-trigger and wait_for_ci_gate would block forever.
        next_node = "wait_for_ci_gate" if unpushed else "human_review_gate"

        return update_state_timestamp(
            {
                **state,
                "revision_requested": False,
                "feedback_comment": None,
                "review_response_posted": False,
                "contested_comments": [],
                "current_node": next_node,
                "is_paused": next_node == "human_review_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"implement_review failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "implement_review")
        return {
            **state,
            "last_error": str(e),
            "current_node": "implement_review",
            "retry_count": state.get("retry_count", 0) + 1,
        }


async def _post_review_objection(
    state: Any,
    objections: str,
    owner: str,
    repo: str,
    pr_number: int | None,
) -> None:
    """Post the agent's review objections to the PR and Jira."""
    ticket_key = state.get("ticket_key", "")
    try:
        github = GitHubClient()
        jira = JiraClient()
        try:
            comment = (
                f"**Forge review response for {ticket_key}:**\n\n"
                f"{objections}\n\n"
                f"*Please confirm whether to proceed as requested or withdraw.*"
            )
            if pr_number:
                await github.create_issue_comment(owner, repo, pr_number, comment)
            await jira.add_comment(
                ticket_key,
                f"Forge has concerns about the PR review feedback. "
                f"Objection posted on PR #{pr_number}. Awaiting confirmation.",
            )
        finally:
            await github.close()
            await jira.close()
    except Exception as e:
        logger.warning(f"Failed to post review objection: {e}")
