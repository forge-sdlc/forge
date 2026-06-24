"""Shared code review utilities used across workflow nodes.

Provides two reusable operations:
  - run_post_change_review: runs the local-review container skill after any
    code-changing step, commits fixes in-place
  - sync_pr_description: updates the PR body if commit messages contradict it
"""

import logging
from pathlib import Path
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def run_post_change_review(
    workspace_path: str,
    ticket_key: str,
    current_repo: str,
    branch_name: str,
    spec_content: str = "",
    guardrails: str = "",
    label: str = "post-change",
) -> bool:
    """Run the local-review container skill after a code-changing step.

    Mirrors what local_reviewer.py does: runs the review in a container,
    commits any in-place fixes it makes. Non-blocking — failures are logged
    and the caller proceeds regardless.

    Args:
        workspace_path: Absolute path to the git workspace.
        ticket_key: Jira ticket key (for commit messages and logging).
        current_repo: Full repo name (owner/repo).
        branch_name: Current branch name.
        spec_content: Spec to guide the review (optional).
        guardrails: Repository guidelines (optional).
        label: Short label for log messages (e.g. "ci-fix", "post-change").

    Returns:
        True if the review committed any fixes, False otherwise.
    """
    settings = get_settings()
    try:
        task_description = load_prompt(
            "local-review",
            workspace_path=workspace_path,
            spec_content=spec_content[:3000] if spec_content else "Not available",
            guardrails=guardrails[:2000] if guardrails else "",
        )

        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=Path(workspace_path),
            task_summary=f"Post-{label} code review",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-review-{label}",
            repo_name=current_repo,
        )

        git = GitOperations(
            Workspace(
                path=Path(workspace_path),
                repo_name=current_repo,
                branch_name=branch_name,
                ticket_key=ticket_key,
            )
        )

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] fix: address issues found in {label} review")
            logger.info(f"Committed {label} review fixes for {ticket_key}")
            return True

        logger.info(f"Post-{label} review: no fixes needed for {ticket_key}")
        return False

    except Exception as e:
        logger.warning(f"Post-{label} review failed (non-fatal): {e}")
        return False


async def sync_pr_description(
    state: Any,
    git: Any,
    owner: str,
    repo: str,
    pr_number: int | None,
    attempt: int,
) -> None:
    """Update the PR description if commit messages contradict any stated facts.

    Uses commit messages (not raw diff) as the source of truth — they are
    already a curated summary of what changed. Errors are swallowed so this
    never blocks any workflow step.

    Args:
        state: Current workflow state (for ticket_key and audit comment).
        git: GitOperations instance for the workspace.
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number, or None to skip.
        attempt: Which code-change attempt this follows (0 = initial PR creation).
    """
    if pr_number is None:
        return

    try:
        commit_log = git._run_git(
            "log",
            "origin/main..HEAD",
            "--pretty=format:%s%n%b",
            "--no-merges",
            check=False,
        ).stdout.strip()

        if not commit_log:
            logger.debug("PR description sync skipped — no commits on branch")
            return

        github = GitHubClient()
        jira = JiraClient()
        try:
            pr_data = await github.get_pull_request(owner, repo, pr_number)
            current_body = pr_data.get("body", "") or ""

            prompt = load_prompt(
                "sync-pr-description",
                current_description=current_body,
                commit_log=commit_log,
            )
            agent = ForgeAgent(get_settings())
            try:
                updated_body = await agent.run_task(
                    task="sync-pr-description",
                    prompt=prompt,
                    context={"owner": owner, "repo": repo, "pr_number": pr_number},
                    trace_context={
                        "ticket_key": state.get("ticket_key", ""),
                        "ticket_type": state.get("ticket_type", ""),
                        "current_node": state.get("current_node", ""),
                        "ci_status": state.get("ci_status", ""),
                        "event_type": state.get("event_type", ""),
                        "event_source": state.get("context", {}).get("source", ""),
                        "retry_count": state.get("retry_count", 0),
                    },
                    include_tools=False,
                )
            finally:
                await agent.close()

            if updated_body:
                updated_body = agent._strip_preamble(updated_body)
            if updated_body and updated_body.strip() != current_body.strip():
                await github.update_pull_request(owner, repo, pr_number, body=updated_body)
                ticket_key = state.get("ticket_key", "")
                label = f"CI fix attempt {attempt}" if attempt > 0 else "PR creation"
                await jira.add_comment(
                    ticket_key,
                    f"PR description updated to reflect changes ({label}).",
                )
                logger.info(f"PR #{pr_number} description synced after {label}")
            else:
                logger.debug(f"PR #{pr_number} description already accurate — no update needed")
        finally:
            await github.close()
            await jira.close()

    except Exception as e:
        logger.warning(f"PR description sync failed (non-fatal): {e}")
