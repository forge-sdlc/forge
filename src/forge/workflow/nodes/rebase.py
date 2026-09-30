"""Rebase node — merges the PR target branch into the PR branch and resolves conflicts.

Triggered by the `/forge rebase` PR comment command.  Works from any
workflow stage: the worker saves the current node in `rebase_return_node`
before routing here, and the node restores it on completion so the
workflow resumes where it left off.
"""

import contextlib
import logging

from forge.config import get_settings
from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    RepositoryRef,
    SourceControlProvider,
)
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.effect_runtime import JiraClient, push_repository
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.nodes.workspace_setup import (
    get_workspace_manager,
    write_workspace_identity,
)
from forge.workflow.sandbox_execution import execute_sandbox_kwargs
from forge.workflow.utils import merge_review_exhaustion, update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.source_control import get_adapter, identity_for
from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)


async def _fetch_pr_body(
    adapter: SourceControlProvider, repo_ref: RepositoryRef, identity: ChangeRequestIdentity
) -> str:
    """Fetch the current PR description, used as context for conflict resolution."""
    change_request = await adapter.get_change_request(repo_ref, identity)
    return change_request.body


async def rebase_pr(state: WorkflowState) -> WorkflowState:
    """Merge the PR target into the PR branch, resolving conflicts with AI if needed.

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

    if not current_repo or not pr_number:
        logger.error(f"Cannot rebase {ticket_key}: missing PR state")
        return update_state_timestamp(
            {
                **state,
                "current_node": rebase_return_node,
                "rebase_return_node": None,
                "last_error": "Cannot rebase: missing repo or PR number in workflow state",
            }
        )
    use_fork = bool(fork_owner and fork_repo)
    push_remote = "fork" if use_fork else "origin"

    settings = get_settings()
    jira = JiraClient()

    try:
        repo_ref, adapter = get_adapter(current_repo)
        identity = identity_for(repo_ref, pr_number)

        # Set up workspace: clone, add fork remote, checkout PR branch
        manager = get_workspace_manager()
        workspace = manager.create_workspace(repo_name=current_repo, ticket_key=ticket_key)
        git = GitOperations(workspace, await adapter.get_git_credentials(repo_ref))
        git.clone()
        write_workspace_identity(
            workspace.path,
            ticket_key=ticket_key,
            repo_name=current_repo,
        )
        if use_fork:
            git.add_fork_remote(fork_owner, fork_repo)

        if git.remote_branch_exists(workspace.branch_name, remote=push_remote):
            git.checkout_branch(workspace.branch_name, remote=push_remote)
        else:
            logger.error(f"Branch {workspace.branch_name} not found on {push_remote}")
            return update_state_timestamp(
                {
                    **state,
                    "current_node": rebase_return_node,
                    "rebase_return_node": None,
                    "last_error": (
                        f"Branch {workspace.branch_name} not found on {push_remote} "
                        f"{fork_owner}/{fork_repo}"
                        if use_fork
                        else f"Branch {workspace.branch_name} not found on {push_remote}"
                    ),
                }
            )

        change_request = await adapter.get_change_request(repo_ref, identity)
        base_branch = change_request.target_branch
        if not isinstance(base_branch, str) or not base_branch:
            raise ValueError("Cannot resolve the PR target branch")
        base_ref = f"origin/{base_branch}"
        # Merge the actual PR target, which may differ from the default branch.
        git._run_git("fetch", "origin", base_branch)
        merge_result = git._run_git("merge", base_ref, check=False)

        if merge_result.returncode == 0:
            if "Already up to date" in merge_result.stdout:
                logger.info(f"{ticket_key}: branch already up to date with {base_branch}")
                await post_status_comment(
                    jira,
                    ticket_key,
                    f"Branch is already up to date with {base_branch} — no rebase needed.",
                )
                return update_state_timestamp(
                    {
                        **state,
                        "current_node": rebase_return_node,
                        "rebase_return_node": None,
                    }
                )

            # Clean merge — push it
            logger.info(f"{ticket_key}: clean merge with {base_branch}, pushing")
            await push_repository(git, use_fork=use_fork, force=True, check_conflicts=False)

            await adapter.create_comment(
                repo_ref,
                identity,
                f"Branch has been rebased onto {base_branch} (no conflicts). CI should re-run.",
            )
            await post_status_comment(
                jira,
                ticket_key,
                f"Branch rebased onto {base_branch} (clean merge) via `/forge rebase` on PR #{pr_number}.",
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
            pr_description = change_request.body
            diff_result = git._run_git(
                "diff",
                "--name-only",
                f"{base_ref}...{push_remote}/{workspace.branch_name}",
                check=False,
            )
            changed_files = diff_result.stdout.strip()
        except Exception as e:
            logger.warning(f"Could not fetch PR context for conflict resolution: {e}")

        # Spawn container to resolve conflicts
        prompt = load_prompt(
            "rebase-pr",
            ticket_key=ticket_key,
            base_branch=base_branch,
            conflicted_files="\n".join(f"- {f}" for f in conflicted_files),
            pr_description=pr_description or "(not available)",
            changed_files=changed_files or "(not available)",
        )

        runner = ContainerRunner(settings)
        result = await execute_sandbox_kwargs(
            state,
            runner=runner,
            discriminator="rebase",
            workspace_path=workspace.path,
            task_summary=f"Resolve merge conflicts with {base_branch} for {ticket_key}",
            task_description=prompt,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-rebase",
            repo_name=current_repo,
            step_name="rebase",
            base_ref=base_ref,
            policy_key="rebase",
        )

        state = merge_review_exhaustion(state, result, ticket_key, "rebase")

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
            git.commit(f"[{ticket_key}] merge: resolve conflicts with {base_branch}")

        await push_repository(git, use_fork=use_fork, force=True, check_conflicts=False)
        logger.info(f"{ticket_key}: conflicts resolved and pushed")

        await adapter.create_comment(
            repo_ref,
            identity,
            f"Merge conflicts resolved and pushed. The PR branch has been updated.\n\n"
            f"Resolved files: {', '.join(f'`{f}`' for f in conflicted_files)}",
        )
        await post_status_comment(
            jira,
            ticket_key,
            f"Merge conflicts with {base_branch} resolved via `/forge rebase` on PR #{pr_number}.\n"
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
