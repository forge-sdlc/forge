"""Workspace setup node for LangGraph workflow."""

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.workflow.nodes.git_persistence import push_to_fork_with_retry
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    set_implementing_label,
    transition_tasks_to_in_progress,
)
from forge.workspace.git_ops import GitOperations
from forge.workspace.guardrails import GuardrailsLoader
from forge.workspace.manager import Workspace, WorkspaceManager

WorkflowState = dict[str, Any]

logger = logging.getLogger(__name__)

_WORKSPACE_IDENTITY_FILE = ".forge/workspace.json"


def write_workspace_identity(path: Path, *, ticket_key: str, repo_name: str) -> None:
    """Persist identity used to safely recover a workspace after process loss."""
    identity_path = path / _WORKSPACE_IDENTITY_FILE
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps({"ticket_key": ticket_key, "repo_name": repo_name}, sort_keys=True) + "\n"
    )


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

    manager = WorkspaceManager(base_dir=get_settings().workspace_base_dir)
    workspace_obj = manager.create_workspace(repo_name=current_repo, ticket_key=ticket_key)
    target_path = workspace_obj.path
    stale_path = Path(stale_workspace_path) if stale_workspace_path else None

    # Build and validate the replacement beside the target.  The existing
    # workspace may contain the only copy of an unpushed commit, so it must not
    # be removed merely because fetch/rebase failed.
    target_path.parent.mkdir(parents=True, exist_ok=True)
    replacement_path = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}-replacement-", dir=target_path.parent)
    )
    replacement_workspace = Workspace(
        path=replacement_path,
        repo_name=current_repo,
        branch_name=branch_name,
        ticket_key=ticket_key,
    )
    git = GitOperations(replacement_workspace)
    try:
        git.clone()
        git.add_fork_remote(fork_owner, fork_repo)
        git.checkout_branch(branch_name, remote="fork")
    except Exception:
        shutil.rmtree(replacement_path, ignore_errors=True)
        raise

    old_path = stale_path if stale_path and stale_path.exists() else None
    backup_path: Path | None = None
    try:
        if old_path:
            backup_path = Path(
                tempfile.mkdtemp(prefix=f".{target_path.name}-old-", dir=target_path.parent)
            )
            backup_path.rmdir()
            old_path.rename(backup_path)
        elif target_path.exists() and target_path != replacement_path:
            # create_workspace creates the deterministic target directory when
            # recovery starts without a stale workspace.
            target_path.rmdir()

        replacement_path.rename(target_path)
    except Exception:
        if backup_path and backup_path.exists() and not target_path.exists():
            backup_path.rename(target_path)
        shutil.rmtree(replacement_path, ignore_errors=True)
        raise

    if backup_path and backup_path.exists():
        shutil.rmtree(backup_path)

    git.workspace.path = target_path
    git.workspace_recreated = True
    write_workspace_identity(target_path, ticket_key=ticket_key, repo_name=current_repo)
    logger.info(f"Workspace recreated at {target_path} for {ticket_key}")
    return str(target_path), git


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

        # Detect the upstream default branch and establish the durable fork
        # before implementation starts.  Every implementation/review handoff
        # pushes to this fork, so continuing without it would guarantee a
        # later persistence failure.
        default_branch = "main"
        fork_owner = state.get("fork_owner", "")
        fork_repo_name = state.get("fork_repo", "")
        if current_repo and "/" in current_repo:
            owner, repo_name = current_repo.split("/", 1)
            github = GitHubClient()
            try:
                try:
                    repo_data = await github.get_repository(owner, repo_name)
                    default_branch = repo_data.get("default_branch", "main")
                    logger.info(f"Detected default branch for {current_repo}: {default_branch}")
                except Exception as exc:
                    logger.warning(f"Could not detect default branch for {current_repo}: {exc}")

                if not fork_owner or not fork_repo_name:
                    fork_data = await github.get_or_create_fork(owner, repo_name)
                    fork_owner = fork_data["owner"]["login"]
                    fork_repo_name = fork_data["name"]

                await github.sync_fork_with_upstream(
                    fork_owner,
                    fork_repo_name,
                    branch=default_branch,
                )
            finally:
                await github.close()

        # Set up feature branch.
        git.add_fork_remote(fork_owner, fork_repo_name)
        branch_exists_on_fork = git.remote_branch_exists(workspace.branch_name, remote="fork")
        if branch_exists_on_fork:
            logger.info(
                f"Branch '{workspace.branch_name}' exists on fork "
                f"{fork_owner}/{fork_repo_name} — checking it out"
            )
            git.checkout_branch(workspace.branch_name, remote="fork")
        else:
            git.create_branch(default_branch)
            # The next graph node may run on a worker that cannot see this
            # local workspace.  Publish the new branch before checkpointing so
            # that worker can recreate it from the fork on the first attempt.
            await push_to_fork_with_retry(git)

        # Create .forge directory for task handoff
        forge_dir = workspace.path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        (forge_dir / "history").mkdir(exist_ok=True)
        write_workspace_identity(
            workspace.path,
            ticket_key=ticket_key,
            repo_name=current_repo,
        )

        # Keep Forge handoff files local to this clone without modifying the
        # target repository's tracked .gitignore.
        exclude_path = workspace.path / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_content = exclude_path.read_text() if exclude_path.exists() else ""
        if ".forge/" not in exclude_content:
            if exclude_content and not exclude_content.endswith("\n"):
                exclude_content += "\n"
            exclude_content += "\n# Forge workflow state (do not commit)\n.forge/\n"
            exclude_path.write_text(exclude_content)

        logger.info("Created .forge directory for task handoff")

        # Load guardrails
        loader = GuardrailsLoader(workspace.path)
        guardrails = loader.load()

        # Store guardrails context in state
        context: dict[str, Any] = state.get("context", {})
        context["guardrails"] = guardrails.get_system_context()
        context["current_repo"] = current_repo
        context["branch_name"] = workspace.branch_name
        context["default_branch"] = default_branch

        logger.info(f"Workspace ready: {workspace}")

        return update_state_timestamp(
            {
                **state,
                "workspace_path": str(workspace.path),
                "current_repo": current_repo,
                "fork_owner": fork_owner,
                "fork_repo": fork_repo_name,
                "context": context,
                "current_node": "implementation",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Workspace setup failed for {ticket_key}: {e}")
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
        else:
            # The manager registry is process-local. A restart or worker handoff
            # can therefore leave a valid workspace on disk without a registry
            # entry. Fall back to the path persisted in workflow state, but only
            # after verifying that it is one of Forge's workspace directories.
            path = Path(workspace_path)
            settings = get_settings()
            allowed_parent = (
                Path(settings.workspace_base_dir)
                if settings.workspace_base_dir
                else Path(tempfile.gettempdir())
            ).resolve()
            resolved_path = path.resolve()
            expected_prefix = f"forge-{ticket_key}-"
            identity_path = resolved_path / _WORKSPACE_IDENTITY_FILE
            try:
                identity = json.loads(identity_path.read_text())
            except (OSError, json.JSONDecodeError):
                identity = None

            if (
                path.is_symlink()
                or not path.is_dir()
                or resolved_path.parent != allowed_parent
                or not resolved_path.name.startswith(expected_prefix)
                or not isinstance(identity, dict)
                or identity.get("ticket_key") != ticket_key
                or identity.get("repo_name") != current_repo
            ):
                # Caught by the outer except — records last_error and
                # clears workspace_path without deleting the directory.
                raise ValueError(
                    f"Refusing to destroy unrecognized workspace path: {workspace_path}"
                )

            recovered_workspace = Workspace(
                path=resolved_path,
                repo_name=current_repo,
                branch_name=state.get("context", {}).get("branch_name", ""),
                ticket_key=ticket_key,
            )
            manager.destroy_workspace(recovered_workspace)
            logger.info("Destroyed unregistered workspace: %s", resolved_path)

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
