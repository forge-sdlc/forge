"""Workspace setup node for LangGraph workflow."""

import logging
import shutil
from pathlib import Path
from typing import Any

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    set_implementing_label,
    transition_tasks_to_in_progress,
)
from forge.workspace.git_ops import GitOperations
from forge.workspace.guardrails import GuardrailsLoader
from forge.workspace.manager import Workspace, WorkspaceManager

logger = logging.getLogger(__name__)


def _recreate_workspace_from_fork(
    *,
    ticket_key: str,
    current_repo: str,
    branch_name: str,
    fork_owner: str,
    fork_repo: str,
    stale_workspace_path: str | None = None,
) -> tuple[str, GitOperations]:
    if not branch_name or not current_repo or not fork_owner or not fork_repo:
        raise ValueError(
            f"Cannot recreate workspace for {ticket_key}: "
            "missing branch_name, current_repo, fork_owner, or fork_repo in state"
        )

    if stale_workspace_path:
        stale_path = Path(stale_workspace_path)
        if stale_path.exists():
            logger.warning(
                "Removing existing workspace for %s before recreating from fork: %s",
                ticket_key,
                stale_path,
            )
            shutil.rmtree(stale_path)

    manager = WorkspaceManager(base_dir=get_settings().workspace_base_dir)
    workspace_obj = manager.create_workspace(repo_name=current_repo, ticket_key=ticket_key)
    git = GitOperations(workspace_obj)
    git.clone()
    git.add_fork_remote(fork_owner, fork_repo)
    git.checkout_branch(branch_name, remote="fork")
    logger.info(f"Workspace recreated at {workspace_obj.path} for {ticket_key}")
    return str(workspace_obj.path), git


def prepare_workspace(
    state: WorkflowState,
    remote: str = "fork",
) -> tuple[str, GitOperations]:
    """Return a workspace path and GitOperations aligned with the remote.

    If the workspace recorded in state already exists on disk, the branch is
    rebased onto the remote so that subsequent pushes cannot be rejected as
    non-fast-forward. If the workspace is missing it is recreated from the
    fork branch via a fresh clone.

    This is the single canonical entry point for all implementation nodes
    (implement_review, attempt_ci_fix, etc.) instead of duplicating
    workspace-recreation and pull logic in each one.

    Args:
        state: Current workflow state.
        remote: Remote name to sync with when the workspace exists (default: 'fork').

    Returns:
        Tuple of (workspace_path, GitOperations).

    Raises:
        ValueError: If the workspace cannot be recreated due to missing state.
        Exception: Any git error encountered during fetch/rebase/clone.
    """
    workspace_path = state.get("workspace_path", "")
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")
    fork_owner = state.get("fork_owner", "")
    fork_repo = state.get("fork_repo", "")
    ticket_key = state["ticket_key"]

    if workspace_path and Path(workspace_path).exists():
        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo,
            branch_name=branch_name,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace)
        try:
            git.pull_rebase(remote=remote)
        except Exception as e:
            logger.warning(
                "Workspace sync failed for %s; recreating workspace from fork: %s",
                ticket_key,
                e,
            )
            return _recreate_workspace_from_fork(
                ticket_key=ticket_key,
                current_repo=current_repo,
                branch_name=branch_name,
                fork_owner=fork_owner,
                fork_repo=fork_repo,
                stale_workspace_path=workspace_path,
            )
        return workspace_path, git

    # Workspace is missing — recreate from fork branch.
    return _recreate_workspace_from_fork(
        ticket_key=ticket_key,
        current_repo=current_repo,
        branch_name=branch_name,
        fork_owner=fork_owner,
        fork_repo=fork_repo,
    )


# Global workspace manager instance
_workspace_manager: WorkspaceManager | None = None


def get_workspace_manager() -> WorkspaceManager:
    """Get the global workspace manager."""
    global _workspace_manager
    if _workspace_manager is None:
        _workspace_manager = WorkspaceManager(base_dir=get_settings().workspace_base_dir)
    return _workspace_manager


async def setup_workspace(state: WorkflowState) -> WorkflowState:
    """Set up an ephemeral workspace for code execution.

    This node:
    1. Creates a temporary workspace directory
    2. Clones the target repository
    3. Creates a feature branch
    4. Loads guardrails (constitution/agents.md)
    5. Stores workspace path in state

    Args:
        state: Current workflow state with tasks_by_repo.

    Returns:
        Updated state with workspace_path set.
    """
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repo")
    tasks_by_repo = state.get("tasks_by_repo", {})

    # Determine which repo to set up
    if not current_repo:
        # Pick the first repository with tasks
        repos = list(tasks_by_repo.keys())
        if not repos:
            logger.error(f"No repositories found for {ticket_key}")
            return {
                **state,
                "last_error": "No repositories to process",
                "current_node": "setup_workspace",
            }
        current_repo = repos[0]

    # Validate repository name
    if current_repo == "unknown" or "/" not in current_repo:
        logger.error(
            f"Invalid repository name '{current_repo}' for {ticket_key}. "
            "Repository must be in 'owner/repo' format."
        )
        return {
            **state,
            "last_error": f"Invalid repository '{current_repo}'. Tasks must specify a valid 'owner/repo' format.",
            "current_node": "setup_workspace",
        }

    logger.info(f"Setting up workspace for {current_repo} ({ticket_key})")

    # Extract repo name for display (handle "owner/repo" format)
    repo_display = (
        current_repo.split("/")[-1]
        if current_repo and "/" in current_repo
        else "unknown repository"
    )

    # Post initial status comment and update Jira labels/transitions
    jira_client = JiraClient()
    try:
        await post_status_comment(
            jira_client,
            ticket_key,
            f"⚙️ Implementation starting for {repo_display}. Setting up workspace...",
        )
        await set_implementing_label(jira_client, ticket_key)

        # Transition task tickets to In Progress if present
        task_keys = state.get("task_keys", [])
        if task_keys:
            await transition_tasks_to_in_progress(jira_client, task_keys)
    finally:
        await jira_client.close()

    manager = get_workspace_manager()

    try:
        # Create workspace
        logger.info(f"Creating workspace directory for {current_repo}...")
        workspace = manager.create_workspace(
            repo_name=current_repo,
            ticket_key=ticket_key,
        )
        logger.info(f"Workspace directory created: {workspace.path}")

        # Initialize git operations
        logger.info(f"Initializing git operations for {workspace}")
        git = GitOperations(workspace)

        # Clone repository (600s timeout)
        logger.info(
            f"Starting clone of {current_repo} (this may take several minutes for large repos)..."
        )
        git.clone()
        logger.info(f"Clone completed successfully for {current_repo}")

        # Set up feature branch.
        # If the workflow already created a PR (fork_owner/fork_repo in state),
        # the branch lives on the fork. Add the fork remote, check whether the
        # branch exists there, and check it out so we don't lose history.
        fork_owner = state.get("fork_owner", "")
        fork_repo_name = state.get("fork_repo", "")

        if fork_owner and fork_repo_name:
            git.add_fork_remote(fork_owner, fork_repo_name)
            branch_exists_on_fork = git.remote_branch_exists(workspace.branch_name, remote="fork")
            if branch_exists_on_fork:
                logger.info(
                    f"Branch '{workspace.branch_name}' exists on fork "
                    f"{fork_owner}/{fork_repo_name} — checking it out"
                )
                git.checkout_branch(workspace.branch_name, remote="fork")
            else:
                git.create_branch()
        else:
            git.create_branch()

        # Create .forge directory for task handoff
        forge_dir = workspace.path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        (forge_dir / "history").mkdir(exist_ok=True)

        # Ensure .forge/ is in .gitignore to prevent accidental commits
        gitignore_path = workspace.path / ".gitignore"
        if gitignore_path.exists():
            content = gitignore_path.read_text()
            if ".forge" not in content:
                if not content.endswith("\n"):
                    content += "\n"
                content += "\n# Forge workflow state (do not commit)\n.forge/\n"
                gitignore_path.write_text(content)
        else:
            gitignore_path.write_text("# Forge workflow state (do not commit)\n.forge/\n")

        logger.info("Created .forge directory for task handoff")

        # Load guardrails
        loader = GuardrailsLoader(workspace.path)
        guardrails = loader.load()

        # Store guardrails context in state
        context: dict[str, Any] = state.get("context", {})
        context["guardrails"] = guardrails.get_system_context()
        context["current_repo"] = current_repo
        context["branch_name"] = workspace.branch_name

        logger.info(f"Workspace ready: {workspace}")

        return update_state_timestamp(
            {
                **state,
                "workspace_path": str(workspace.path),
                "current_repo": current_repo,
                "context": context,
                "current_node": "implementation",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Workspace setup failed for {ticket_key}: {e}")
        # Post error notification to Jira
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "setup_workspace")
        return {
            **state,
            "last_error": str(e),
            "current_node": "setup_workspace",
            "retry_count": state.get("retry_count", 0) + 1,
        }


async def teardown_workspace(state: WorkflowState) -> WorkflowState:
    """Tear down the workspace after PR creation.

    Args:
        state: Current workflow state with workspace_path.

    Returns:
        Updated state with workspace_path cleared.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")

    if not workspace_path:
        logger.debug(f"No workspace to tear down for {ticket_key}")
        return state

    logger.info(f"Tearing down workspace for {ticket_key}")

    manager = get_workspace_manager()

    try:
        current_repo = state.get("current_repo", "")
        workspace = manager.get_workspace(ticket_key, current_repo)

        if workspace:
            manager.destroy_workspace(workspace)
            logger.info(f"Workspace destroyed: {workspace}")

        return update_state_timestamp(
            {
                **state,
                "workspace_path": None,
                "current_node": "workspace_complete",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Workspace teardown failed for {ticket_key}: {e}")
        # Don't fail the workflow on teardown errors
        return {
            **state,
            "workspace_path": None,
            "last_error": f"Teardown warning: {e}",
        }
