"""implement_review node — addresses PR review feedback on an existing branch."""

import json
import logging
from pathlib import Path
from typing import Any

from langgraph.graph import END

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.code_review import run_post_change_review, sync_pr_description
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.utils import merge_review_exhaustion, set_paused, update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.review_decisions import (
    merge_review_decisions,
    reply_to_review_decisions,
)
from forge.workflow.utils.source_control import get_adapter, identity_for
from forge.workspace.handoff import capture_handoff

logger = logging.getLogger(__name__)

_REVIEW_COMMENTS_FILE = ".forge/review-comments.md"
_REVIEW_PLAN_FILE = ".forge/review-plan.md"
_REVIEW_OBJECTIONS_FILE = ".forge/review-objections.md"
_REVIEW_DECISIONS_FILE = ".forge/review-decisions.json"
_REVIEW_ADDRESSING_COMMENT = "Forge is addressing PR review feedback now."


def review_response_gate(state: WorkflowState) -> WorkflowState:
    """Pause workflow awaiting human confirmation of contested review comments."""
    ticket_key = state["ticket_key"]

    if state.get("pr_merged"):
        logger.info(f"Review response gate: PR already merged for {ticket_key}, skipping pause")
        return update_state_timestamp(
            {**state, "current_node": "review_response_gate", "is_paused": False}
        )

    logger.info(f"Review response gate: pausing for {ticket_key}")
    return set_paused(state, "review_response_gate")


def route_review_response(state: WorkflowState) -> str:
    """Route after human responds to the agent's review objection."""
    if state.get("is_paused"):
        return END

    revision_requested = state.get("revision_requested", False)

    # A response may confirm one thread while other contested threads remain.
    # Re-run analysis so accepted work proceeds independently of those threads.
    if revision_requested:
        return "implement_review"

    return "human_review_gate"


def _thread_is_settled(
    thread: dict[str, Any],
    disposition: str | None,
    bot_login: str,
    prefix: str | None,
) -> bool:
    """A thread needs no further analysis until something new happens in it.

    Accepted/ignored threads are settled permanently. Contested/clarified
    threads are settled only while Forge's own reply is still the last word —
    once a human comments after it, the thread is live again.
    """
    if disposition in ("accept", "ignore"):
        return True
    if disposition not in ("contest", "clarify"):
        return False
    if not bot_login:
        # An unresolved identity must never coincidentally match an empty
        # author (e.g. a deleted account) — fail toward re-analysis.
        return False
    comments = sorted(thread.get("comments", []), key=lambda c: c.get("created_at") or "")
    if not comments:
        return False
    last_comment = comments[-1]
    sender_login = last_comment.get("author", "")
    comment_body = last_comment.get("body", "")
    return sender_login.casefold() == bot_login.casefold() or bool(
        prefix and comment_body.startswith(prefix)
    )


async def _fetch_pr_review_comments(
    current_repo: str,
    pr_number: int,
    review_body: str,
    review_comments: list[dict[str, Any]] | set[str] | None = None,
) -> str:
    """Fetch all PR review comments and format them for the analysis container.

    Combines the review summary body with all inline review comments so the
    analysis agent has the full picture. Threads already settled (accepted,
    ignored, or contested with no human reply since Forge's own response) are
    excluded so the analysis agent isn't re-fed the same resolved feedback.

    Args:
        current_repo: Repository identifier (owner/repo or repos.yaml id).
        pr_number: PR number.
        review_body: The review summary body from the webhook.
        review_comments: Prior per-thread decisions from earlier review cycles.

    Returns:
        Formatted markdown string of all review feedback.
    """
    try:
        repo_ref, adapter = get_adapter(current_repo)
        identity = identity_for(repo_ref, pr_number)
        reviews = await adapter.get_review_thread_comments(repo_ref, identity)
    except Exception as e:
        logger.warning(f"Could not fetch inline review comments: {e}")
        reviews = []

    processed_thread_ids = review_comments if isinstance(review_comments, set) else set()
    threads: list[dict[str, Any]] = [
        {
            "thread_id": review.id,
            "path": review.comments[0].path if review.comments else "",
            "line": review.comments[0].line if review.comments else None,
            "comments": [
                {"comment_id": c.id, "body": c.body, "author": c.author} for c in review.comments
            ],
        }
        for review in reviews
        if review.id not in processed_thread_ids
    ]

    lines = ["# PR Review Feedback\n"]

    if review_body and review_body.strip():
        lines.append("## Review Summary\n")
        lines.append(review_body.strip())
        lines.append("\n")

    prior_decisions = review_comments if isinstance(review_comments, list) else []
    dispositions_by_thread = {
        item["thread_id"]: item.get("disposition")
        for item in prior_decisions
        if item.get("thread_id")
    }

    bot_login = ""
    if any(
        dispositions_by_thread.get(thread["thread_id"]) in ("contest", "clarify")
        for thread in threads
    ):
        try:
            repo_ref, adapter = get_adapter(current_repo)
            bot_login = (await adapter.get_authenticated_identity(repo_ref)).login
        except Exception as e:
            logger.warning(f"Could not resolve Forge's bot login: {e}")

    prefix = get_settings().forge_bot_comment_prefix
    threads = [
        thread
        for thread in threads
        if not _thread_is_settled(
            thread, dispositions_by_thread.get(thread["thread_id"]), bot_login, prefix
        )
    ]
    if threads:
        lines.append("## Unresolved Review Threads\n")
        for thread in threads:
            lines.append(
                f"### Thread `{thread['thread_id']}` — `{thread['path']}` "
                f"(line {thread.get('line', '?')})\n"
            )
            for comment in thread["comments"]:
                lines.append(
                    f"#### Comment `{comment['comment_id']}` by @{comment.get('author', 'unknown')}\n"
                )
                lines.append(comment.get("body", "").strip())
                lines.append("\n")

    return "\n".join(lines)


def _load_review_decisions(workspace_path: str) -> list[dict[str, Any]]:
    """Load and validate per-thread decisions produced by review analysis."""
    path = Path(workspace_path) / _REVIEW_DECISIONS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Invalid review decisions file: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    allowed = {"accept", "contest", "clarify", "ignore"}
    return [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("disposition") in allowed
        and isinstance(item.get("thread_id"), str)
    ]


async def _reply_to_review_threads(
    *, current_repo: str, pr_number: int | None, decisions: list[dict[str, Any]]
) -> None:
    """Post decision responses in their originating GitHub review threads."""
    # skip_addressed is defense-in-depth, not the primary guard: decisions here
    # are freshly parsed from review-decisions.json each cycle and never carry
    # a prior "addressed" status, so _thread_is_settled (which keeps a settled
    # thread out of the analysis input entirely) is what actually prevents
    # re-posting today.
    await reply_to_review_decisions(
        current_repo=current_repo,
        pr_number=pr_number,
        decisions=decisions,
        skip_addressed=True,
    )


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
    fix_started = False

    try:
        try:
            workspace_path, git = await prepare_workspace(state)
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
        await _post_review_addressing_comment(
            ticket_key=ticket_key,
            current_repo=current_repo,
            pr_number=pr_number,
        )

        review_comments_text = await _fetch_pr_review_comments(
            current_repo=current_repo,
            pr_number=pr_number or 0,
            review_body=feedback_comment,
            review_comments=state.get("review_comments", []),
        )

        # Write all review comments to a file so the container can read them
        forge_dir = Path(workspace_path) / ".forge"
        forge_dir.mkdir(parents=True, exist_ok=True)
        (forge_dir / "review-comments.md").write_text(review_comments_text)

        # Clear previous analysis files
        for fname in (_REVIEW_PLAN_FILE, _REVIEW_OBJECTIONS_FILE, _REVIEW_DECISIONS_FILE):
            fpath = Path(workspace_path) / fname
            if fpath.exists():
                fpath.unlink()

        # ── Phase 1: Analysis container ───────────────────────────────────────
        # The container reads .forge/review-comments.md, explores the code,
        # and outputs .forge/review-plan.md + .forge/review-objections.md.
        # It makes NO code changes.
        analysis_prompt = load_prompt("implement-review", ticket_key=ticket_key)

        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Analyze PR review feedback for {ticket_key}",
            task_description=analysis_prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-review-analyze",
            repo_name=current_repo,
            step_name="implement_review_analyze",
            policy_key="implement_review_analysis",
            skill_name="implement-review",
        )
        state = merge_review_exhaustion(state, result, ticket_key, "implement_review_analyze")

        # ── Process per-thread dispositions ──────────────────────────────────
        decisions = _load_review_decisions(workspace_path)
        response_decisions = [
            item for item in decisions if item["disposition"] in ("contest", "clarify", "ignore")
        ]
        contested_comments = [
            item for item in decisions if item["disposition"] in ("contest", "clarify")
        ]
        await _reply_to_review_threads(
            current_repo=current_repo,
            pr_number=pr_number,
            decisions=response_decisions,
        )
        # Backward-compatible fallback if an older analysis prompt writes only
        # the legacy objections file. New analysis never blocks accepted work.
        objections_path = Path(workspace_path) / _REVIEW_OBJECTIONS_FILE
        if not decisions and objections_path.exists():
            objections_text = objections_path.read_text().strip()
            if objections_text:
                logger.info(f"Agent contested review comments for {ticket_key}")
                await _post_review_objection(
                    state=state,
                    objections=objections_text,
                    current_repo=current_repo,
                    pr_number=pr_number,
                )
                contested_comments = [{"text": objections_text}]

        # ── Phase 2: Implementation container ────────────────────────────────
        # Only runs if the analysis produced actionable items.
        plan_path = Path(workspace_path) / _REVIEW_PLAN_FILE
        plan_text = plan_path.read_text().strip() if plan_path.exists() else ""

        if not plan_text or plan_text == "# No actionable items":
            logger.info(f"No actionable review items for {ticket_key} — nothing to implement")
        else:
            fix_prompt = load_prompt("implement-review-fix", ticket_key=ticket_key)

            runner = ContainerRunner(settings)
            fix_started = True
            result = await runner.run(
                workspace_path=Path(workspace_path),
                task_summary=f"Implement PR review plan for {ticket_key}",
                task_description=fix_prompt,
                ticket_key=ticket_key,
                task_key=f"{ticket_key}-review-fix",
                repo_name=current_repo,
                step_name="implement_review_fix",
                policy_key="implement_review_fix",
                skill_name="implement-review",
            )
            state = merge_review_exhaustion(state, result, ticket_key, "implement_review_fix")

            state = capture_handoff(workspace_path, current_repo, f"{ticket_key}-review-fix", state)
            fix_started = False

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
            _, review_result = await run_post_change_review(
                workspace_path=workspace_path,
                ticket_key=ticket_key,
                current_repo=current_repo,
                branch_name=branch_name,
                spec_content=state.get("spec_content", ""),
                guardrails=state.get("context", {}).get("guardrails", ""),
                label="review-impl",
            )
            if review_result is not None:
                state = merge_review_exhaustion(state, review_result, ticket_key, "code_review")

            if fork_owner and fork_repo:
                git.push_to_fork(force=False)
            else:
                git.push(force=False)
            logger.info(f"Review implementation pushed for {ticket_key}")

            await sync_pr_description(
                state,
                git,
                current_repo=current_repo,
                pr_number=pr_number,
                attempt=0,
            )
            accepted_response = "Forge implemented this feedback in the latest pushed revision."
        else:
            logger.info(f"No new commits after review implementation for {ticket_key}")
            accepted_response = (
                "Forge verified this feedback; no additional code change was needed."
            )

        accepted_decisions = [item for item in decisions if item["disposition"] == "accept"]
        for decision in accepted_decisions:
            if not decision.get("response"):
                decision["response"] = accepted_response
        await _reply_to_review_threads(
            current_repo=current_repo,
            pr_number=pr_number,
            decisions=accepted_decisions,
        )

        # Contested threads require human arbitration before re-entering the review cycle.
        next_node = "review_response_gate" if contested_comments else "human_review_gate"

        return update_state_timestamp(
            {
                **state,
                "revision_requested": False,
                "feedback_comment": None,
                "review_comments": merge_review_decisions(
                    state.get("review_comments", []), decisions
                ),
                "review_response_posted": bool(contested_comments),
                "contested_comments": contested_comments,
                "current_node": next_node,
                "is_paused": next_node in ("human_review_gate", "review_response_gate"),
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"implement_review failed for {ticket_key}: {e}")
        if fix_started and workspace_path:
            state = capture_handoff(workspace_path, current_repo, f"{ticket_key}-review-fix", state)
        return {
            **state,
            "last_error": str(e),
            "current_node": "implement_review",
            "retry_count": state.get("retry_count", 0) + 1,
        }


async def _post_review_addressing_comment(
    ticket_key: str,
    current_repo: str,
    pr_number: int | None,
) -> None:
    """Post a non-triggering PR status update when review work starts."""
    if not pr_number:
        logger.debug(f"Skipping review addressing PR comment for {ticket_key}: no PR number")
        return

    try:
        repo_ref, adapter = get_adapter(current_repo)
        identity = identity_for(repo_ref, pr_number)
        await adapter.create_comment(repo_ref, identity, _REVIEW_ADDRESSING_COMMENT)
    except Exception as e:
        logger.warning(f"Failed to post review addressing PR status for {ticket_key}: {e}")


async def _post_review_objection(
    state: Any,
    objections: str,
    current_repo: str,
    pr_number: int | None,
) -> None:
    """Post the agent's review objections to the PR and Jira."""
    ticket_key = state.get("ticket_key", "")
    try:
        jira = JiraClient()
        try:
            comment = (
                f"**Forge review response for {ticket_key}:**\n\n"
                f"{objections}\n\n"
                f"*Please confirm whether to proceed as requested or withdraw.*"
            )
            if pr_number:
                repo_ref, adapter = get_adapter(current_repo)
                identity = identity_for(repo_ref, pr_number)
                await adapter.create_comment(repo_ref, identity, comment)
            await post_status_comment(
                jira,
                ticket_key,
                f"Forge has concerns about the PR review feedback. "
                f"Objection posted on PR #{pr_number}. Awaiting confirmation.",
            )
        finally:
            await jira.close()
    except Exception as e:
        logger.warning(f"Failed to post review objection: {e}")
