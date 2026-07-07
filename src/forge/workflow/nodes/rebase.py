"""Rebase node — merges main into the PR branch and resolves conflicts.

Triggered by the `/forge rebase` PR comment command.  Works from any
workflow stage: the worker saves the current node in `rebase_return_node`
before routing here, and the node restores it on completion so the
workflow resumes where it left off.
"""

import contextlib
import logging

from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.workspace_setup import get_workspace_manager
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)


async def rebase_pr(state: WorkflowState) -> WorkflowState:
    """Merge main into the PR branch, resolving conflicts with AI if needed.

    Args:
        state: Current workflow state.

    Returns:
        Updated state routed back to rebase_return_node.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo", "")
    fork_owner = state.get("fork_owner", "")
    fork_repo = state.get("fork_repo", "")
    pr_number = state.get("current_pr_number")
    rebase_return_node = state.get("rebase_return_node", "ci_evaluator")

    if not current_repo or not fork_owner or not fork_repo or not pr_number:
        logger.error(f"Cannot rebase {ticket_key}: missing PR/fork state")
        return update_state_timestamp(
            {
                **state,
                "current_node": rebase_return_node,
                "rebase_return_node": None,
                "last_error": "Cannot rebase: missing PR or fork information in workflow state",
            }
        )

    owner, repo = current_repo.split("/", 1)
    settings = get_settings()
    jira = JiraClient()
    github = GitHubClient()

    try:
        # Set up workspace: clone, add fork remote, checkout PR branch
        manager = get_workspace_manager()
        workspace = manager.create_workspace(repo_name=current_repo, ticket_key=ticket_key)
        git = GitOperations(workspace)
        git.clone()
        git.add_fork_remote(fork_owner, fork_repo)

        if git.remote_branch_exists(workspace.branch_name, remote="fork"):
            git.checkout_branch(workspace.branch_name, remote="fork")
        else:
            logger.error(f"Branch {workspace.branch_name} not found on fork")
            return update_state_timestamp(
                {
                    **state,
                    "current_node": rebase_return_node,
                    "rebase_return_node": None,
                    "last_error": f"Branch {workspace.branch_name} not found on fork {fork_owner}/{fork_repo}",
                }
            )

        # Attempt merge
        git._run_git("fetch", "origin", "main")
        merge_result = git._run_git("merge", "origin/main", check=False)

        if merge_result.returncode == 0:
            if "Already up to date" in merge_result.stdout:
                logger.info(f"{ticket_key}: branch already up to date with main")
                await post_status_comment(
                    jira, ticket_key, "Branch is already up to date with main — no rebase needed."
                )
                return update_state_timestamp(
                    {
                        **state,
                        "current_node": rebase_return_node,
                        "rebase_return_node": None,
                    }
                )

            # Clean merge — push it
            logger.info(f"{ticket_key}: clean merge with main, pushing")
            git.push_to_fork(force=True)

            await github.create_issue_comment(
                owner,
                repo,
                pr_number,
                "Branch has been rebased onto main (no conflicts). CI should re-run.",
            )
            await post_status_comment(
                jira,
                ticket_key,
                f"Branch rebased onto main (clean merge) via `/forge rebase` on PR #{pr_number}.",
            )

            return update_state_timestamp(
                {
                    **state,
                    "workspace_path": str(workspace.path),
                    "current_node": rebase_return_node,
                    "rebase_return_node": None,
                    "last_error": None,
                }
            )

        # Merge conflicts — get conflicted files
        status_result = git._run_git("diff", "--name-only", "--diff-filter=U", check=False)
        conflicted_files = [
            f.strip() for f in status_result.stdout.strip().split("\n") if f.strip()
        ]
        logger.info(f"{ticket_key}: {len(conflicted_files)} conflicted file(s): {conflicted_files}")

        # Get PR description for context
        pr_description = ""
        changed_files = ""
        try:
            pr_data = await github.get_pull_request(owner, repo, pr_number)
            pr_description = pr_data.get("body", "") or ""
            diff_result = git._run_git(
                "diff", "--name-only", f"origin/main...fork/{workspace.branch_name}", check=False
            )
            changed_files = diff_result.stdout.strip()
        except Exception as e:
            logger.warning(f"Could not fetch PR context for conflict resolution: {e}")

        # Spawn container to resolve conflicts
        prompt = load_prompt(
            "rebase-pr",
            ticket_key=ticket_key,
            conflicted_files="\n".join(f"- {f}" for f in conflicted_files),
            pr_description=pr_description or "(not available)",
            changed_files=changed_files or "(not available)",
        )

        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=workspace.path,
            task_summary=f"Resolve merge conflicts with main for {ticket_key}",
            task_description=prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-rebase",
            repo_name=current_repo,
        )

        if result.exit_code != 0:
            logger.error(
                f"Conflict resolution container failed for {ticket_key}: exit {result.exit_code}"
            )
            git._run_git("merge", "--abort", check=False)
            await post_status_comment(
                jira,
                ticket_key,
                f"Conflict resolution failed (container exit code {result.exit_code}). Manual intervention needed.",
            )
            return update_state_timestamp(
                {
                    **state,
                    "current_node": rebase_return_node,
                    "rebase_return_node": None,
                    "last_error": f"Conflict resolution container failed with exit code {result.exit_code}",
                }
            )

        # Verify no conflict markers remain
        check_result = git._run_git("diff", "--check", check=False)
        if check_result.returncode != 0:
            logger.error(f"Conflict markers still present after resolution for {ticket_key}")
            git._run_git("merge", "--abort", check=False)
            await post_status_comment(
                jira,
                ticket_key,
                "Conflict resolution incomplete — conflict markers still present. Manual intervention needed.",
            )
            return update_state_timestamp(
                {
                    **state,
                    "current_node": rebase_return_node,
                    "rebase_return_node": None,
                    "last_error": "Conflict markers remain after AI resolution attempt",
                }
            )

        # Commit and push
        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] merge: resolve conflicts with main")

        git.push_to_fork(force=True)
        logger.info(f"{ticket_key}: conflicts resolved and pushed to fork")

        await github.create_issue_comment(
            owner,
            repo,
            pr_number,
            f"Merge conflicts resolved and pushed. The PR branch has been updated.\n\n"
            f"Resolved files: {', '.join(f'`{f}`' for f in conflicted_files)}",
        )
        await post_status_comment(
            jira,
            ticket_key,
            f"Merge conflicts with main resolved via `/forge rebase` on PR #{pr_number}.\n"
            f"Conflicted files: {', '.join(conflicted_files)}",
        )

        return update_state_timestamp(
            {
                **state,
                "workspace_path": str(workspace.path),
                "current_node": rebase_return_node,
                "rebase_return_node": None,
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Rebase failed for {ticket_key}: {e}", exc_info=True)
        with contextlib.suppress(Exception):
            await post_status_comment(jira, ticket_key, f"Rebase failed: {e}")
        return update_state_timestamp(
            {
                **state,
                "current_node": rebase_return_node,
                "rebase_return_node": None,
                "last_error": f"Rebase failed: {e}",
            }
        )
    finally:
        await jira.close()
        await github.close()
