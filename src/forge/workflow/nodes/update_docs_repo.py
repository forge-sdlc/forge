"""Post-merge docs repo update node.

When forge.docs_repo is set, clones the separate docs repo after code merge,
runs the update agent in a container with both repos mounted, and creates a
fork-based PR for the docs changes. Non-blocking.
"""

import logging
from pathlib import Path
from typing import Any

from forge.config import Settings, get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.skills.utils import extract_project_key
from forge.workflow.nodes.workspace_setup import _configure_forge_exclude
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitError, GitOperations
from forge.workspace.manager import WorkspaceManager

WorkflowState = dict[str, Any]

logger = logging.getLogger(__name__)


async def update_docs_repo(state: WorkflowState) -> WorkflowState:
    """Update documentation in a separate docs repo after code merge.

    Checks forge.docs_repo project property. If not set, skips.
    If set, clones both repos, runs the update agent with the code repo
    mounted read-only, and creates a fork-based PR.

    Non-blocking: failures log a warning and proceed.

    Args:
        state: Current workflow state (after merge).

    Returns:
        Updated state with docs_pr_url if a PR was created.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo", "")

    # Check for separate docs repo
    docs_repo = None
    settings = get_settings()
    try:
        project_key = extract_project_key(ticket_key)
        jira = JiraClient(settings)
        try:
            docs_repo = await jira.get_project_docs_repo(project_key)
        finally:
            await jira.close()
    except Exception as e:
        logger.warning(f"Could not check docs repo config for {ticket_key}: {e}")

    if not docs_repo or docs_repo == current_repo:
        logger.info(f"No separate docs repo for {ticket_key}, skipping")
        return state

    logger.info(f"Updating separate docs repo {docs_repo} for {ticket_key}")

    code_owner, code_repo_name = current_repo.split("/", 1)

    # Resolve the code repo's default branch so the prompt diff command is correct.
    code_default_branch = "main"
    github_for_code = GitHubClient(settings)
    try:
        code_repo_data = await github_for_code.get_repository(code_owner, code_repo_name)
        code_default_branch = code_repo_data.get("default_branch", "main")
        logger.info(f"Code repo {current_repo} default branch: {code_default_branch}")
    except Exception as e:
        logger.warning(f"Could not fetch code repo metadata, defaulting to 'main': {e}")
    finally:
        await github_for_code.close()

    # Resolve the docs repo's actual default branch to avoid hardcoding "main".
    owner, repo = docs_repo.split("/", 1)
    default_branch = "main"
    github_for_meta = GitHubClient(settings)
    try:
        repo_data = await github_for_meta.get_repository(owner, repo)
        default_branch = repo_data.get("default_branch", "main")
        logger.info(f"Docs repo {docs_repo} default branch: {default_branch}")
    except Exception as e:
        logger.warning(f"Could not fetch docs repo metadata, defaulting to 'main': {e}")
    finally:
        await github_for_meta.close()

    guardrails = state.get("context", {}).get("guardrails", "")
    branch_name = state.get("context", {}).get("branch_name", f"forge/{ticket_key.lower()}")
    fork_owner = state.get("fork_owner") or ""
    fork_repo = state.get("fork_repo") or ""

    workspace_manager = WorkspaceManager(base_dir=settings.workspace_base_dir)
    code_workspace = None
    docs_workspace = None
    try:
        code_workspace = workspace_manager.create_workspace(
            repo_name=current_repo,
            ticket_key=ticket_key,
        )
        docs_workspace = workspace_manager.create_workspace(
            repo_name=docs_repo,
            ticket_key=ticket_key,
            branch_name=branch_name,
        )

        # Clone upstream code repo; try to reach the PR branch so the agent
        # can run `git diff origin/{code_default_branch}...HEAD` inside the container.
        code_git = GitOperations(code_workspace)
        code_git.clone()
        try:
            if fork_owner and fork_repo:
                code_git.add_fork_remote(fork_owner, fork_repo)
                code_git.checkout_branch(branch_name, remote="fork")
            else:
                code_git.checkout_branch(branch_name, remote="origin")
        except GitError:
            # Branch was deleted after merge (GitHub auto-delete is common).
            # Fall back to checking out the merge commit directly.
            pr_number = state.get("current_pr_number")
            if pr_number:
                github_for_sha = GitHubClient(settings)
                try:
                    pr_data = await github_for_sha.get_pull_request(
                        code_owner, code_repo_name, int(pr_number)
                    )
                    merge_sha = pr_data.get("merge_commit_sha")
                    if merge_sha:
                        code_git.checkout_commit(merge_sha)
                        logger.info(
                            f"Branch {branch_name} not found; checked out merge commit {merge_sha}"
                        )
                except Exception as e2:
                    logger.warning(f"Could not resolve merge commit SHA for {ticket_key}: {e2}")
                finally:
                    await github_for_sha.close()
            else:
                logger.warning(
                    f"Branch {branch_name} not found and no PR number in state; "
                    "agent will work from default branch HEAD"
                )

        # Clone and set up the docs repo
        docs_git = GitOperations(docs_workspace)
        docs_git.clone()
        docs_git.create_branch(default_branch)

        forge_dir = docs_workspace.path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        _configure_forge_exclude(docs_workspace.path)

        # Run the doc update agent with both repos mounted
        task_description = load_prompt(
            "update-docs-separate",
            workspace_path=str(docs_workspace.path),
            guardrails=guardrails[:2000] if guardrails else "",
            code_default_branch=code_default_branch,
        )

        runner = ContainerRunner(settings)
        await runner.run(
            workspace_path=docs_workspace.path,
            task_summary="Update stale documentation in docs repo",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-docs-repo",
            repo_name=docs_repo,
            extra_mounts=[(code_workspace.path, "/code-repo")],
        )

        # Commit any uncommitted changes
        if docs_git.has_uncommitted_changes():
            docs_git.stage_all()
            docs_git.commit(f"[{ticket_key}] docs: update documentation for code changes")

        # Check if any commits were made ahead of the upstream default branch
        if not _branch_has_commits(docs_workspace.path, default_branch):
            logger.info(f"No doc changes needed in {docs_repo} for {ticket_key}")
            return state

        # Create PR
        docs_pr_url = await _create_docs_pr(
            ticket_key=ticket_key,
            docs_repo=docs_repo,
            git=docs_git,
            branch_name=branch_name,
            base_branch=default_branch,
            settings=settings,
        )
        logger.info(f"Created docs PR for {ticket_key}: {docs_pr_url}")
        return update_state_timestamp({**state, "docs_pr_url": docs_pr_url})

    except Exception as e:
        logger.warning(f"Separate docs repo update failed for {ticket_key}: {e}")
        return state
    finally:
        if docs_workspace is not None:
            workspace_manager.destroy_workspace(docs_workspace)
        if code_workspace is not None:
            workspace_manager.destroy_workspace(code_workspace)


async def _create_docs_pr(
    ticket_key: str,
    docs_repo: str,
    git: GitOperations,
    branch_name: str,
    base_branch: str,
    settings: Settings,
) -> str:
    """Create a fork-based PR for the docs repo."""
    owner, repo = docs_repo.split("/", 1)

    github = GitHubClient(settings)
    jira = JiraClient(settings)
    try:
        fork_data = await github.get_or_create_fork(owner, repo)
        fork_owner = fork_data["owner"]["login"]
        fork_repo = fork_data["name"]

        await github.sync_fork_with_upstream(fork_owner, fork_repo)
        git.add_fork_remote(fork_owner, fork_repo)
        git.push_to_fork()

        pr_data = await github.create_pull_request(
            owner=owner,
            repo=repo,
            title=f"[{ticket_key}] docs: update documentation for code changes",
            body=(
                f"Automated documentation update for {ticket_key}.\n\n"
                f"Code changes in the source repository made some documentation "
                f"files stale. This PR updates them to reflect the current code."
            ),
            head=f"{fork_owner}:{branch_name}",
            base=base_branch,
        )

        pr_url = str(pr_data.get("html_url", ""))
        pr_number = pr_data.get("number", "?")

        await jira.add_comment(
            ticket_key,
            f"Documentation PR created: [{docs_repo}#{pr_number}]({pr_url})",
        )

        return pr_url
    finally:
        await github.close()
        await jira.close()


def _branch_has_commits(workspace_path: Path, base_branch: str = "main") -> bool:
    """Check if the current branch has commits ahead of the upstream default branch."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "log", f"origin/{base_branch}..HEAD", "--oneline"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False
