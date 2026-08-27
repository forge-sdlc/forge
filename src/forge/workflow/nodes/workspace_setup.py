"""Workspace setup node for LangGraph workflow."""

import errno
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.integrations.source_control.errors import NotFoundError, ProviderConfigError
from forge.workflow.nodes.git_persistence import push_to_fork_with_retry
from forge.workflow.planning_state import repository_compatibility_update
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import (
    post_status_comment,
    set_implementing_label,
    transition_tasks_to_in_progress,
)
from forge.workflow.utils.source_control import get_adapter
from forge.workspace.git_ops import GitOperations
from forge.workspace.guardrails import GuardrailsLoader
from forge.workspace.handoff import materialize_handoff
from forge.workspace.manager import Workspace, WorkspaceManager

WorkflowState = dict[str, Any]

logger = logging.getLogger(__name__)

_WORKSPACE_IDENTITY_FILE = ".forge/workspace.json"
_BACKUP_CLEANUP_ATTEMPTS = 3
_BACKUP_CLEANUP_RETRY_DELAY_SECONDS = 0.05


def write_workspace_identity(path: Path, *, ticket_key: str, repo_name: str) -> None:
    """Persist identity used to safely recover a workspace after process loss."""
    identity_path = path / _WORKSPACE_IDENTITY_FILE
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps({"ticket_key": ticket_key, "repo_name": repo_name}, sort_keys=True) + "\n"
    )


def _remove_workspace_backup(path: Path) -> None:
    """Remove a replaced workspace, tolerating short-lived filesystem races."""
    for attempt in range(1, _BACKUP_CLEANUP_ATTEMPTS + 1):
        try:
            WorkspaceManager.remove_path(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt == _BACKUP_CLEANUP_ATTEMPTS:
                raise
            time.sleep(_BACKUP_CLEANUP_RETRY_DELAY_SECONDS)


async def _recreate_workspace_from_fork(
    *,
    ticket_key: str,
    current_repo: str,
    branch_name: str,
    fork_owner: str,
    fork_repo: str,
    state: WorkflowState,
    stale_workspace_path: str | None = None,
) -> tuple[str, GitOperations]:
    if not branch_name or not current_repo:
        raise ValueError(
            f"Cannot recreate workspace for {ticket_key}: "
            "missing branch_name or current_repo in state"
        )
    use_fork = bool(fork_owner and fork_repo)
    repo_ref, adapter = get_adapter(current_repo)
    credentials = await adapter.get_git_credentials(repo_ref)

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
    git = GitOperations(replacement_workspace, credentials)
    try:
        git.clone()
        if use_fork:
            git.add_fork_remote(fork_owner, fork_repo)
            git.checkout_branch(branch_name, remote="fork")
        else:
            git.checkout_branch(branch_name, remote="origin")
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
        try:
            _remove_workspace_backup(backup_path)
        except OSError:
            # The replacement is already installed at target_path. Cleanup is
            # best-effort here: surfacing this error would incorrectly report
            # workspace recovery as failed and hide the error that triggered it.
            logger.warning(
                "Workspace recreated for %s, but backup cleanup failed: %s",
                ticket_key,
                backup_path,
                exc_info=True,
            )

    git.workspace.path = target_path
    git.workspace_recreated = True
    write_workspace_identity(target_path, ticket_key=ticket_key, repo_name=current_repo)
    materialize_handoff(target_path, current_repo, state)
    logger.info(f"Workspace recreated at {target_path} for {ticket_key}")
    return str(target_path), git


async def prepare_workspace(
    state: WorkflowState,
) -> tuple[str, GitOperations]:
    """Return a workspace path and GitOperations aligned with the remote.

    If the workspace recorded in state already exists on disk, the branch is
    rebased onto the remote so that subsequent pushes cannot be rejected as
    non-fast-forward. If the workspace is missing it is recreated via a fresh
    clone, from the fork branch when the repo uses fork mode, or from origin
    directly when it uses direct mode (state has no fork_owner/fork_repo).

    This is the single canonical entry point for all implementation nodes
    (implement_review, attempt_ci_fix, etc.) instead of duplicating
    workspace-recreation and pull logic in each one.

    Args:
        state: Current workflow state.

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
    remote = "fork" if (fork_owner and fork_repo) else "origin"
    ticket_key = state["ticket_key"]
    repo_ref, adapter = get_adapter(current_repo)
    credentials = await adapter.get_git_credentials(repo_ref)

    if workspace_path and Path(workspace_path).exists():
        workspace = Workspace(
            path=Path(workspace_path),
            repo_name=current_repo,
            branch_name=branch_name,
            ticket_key=ticket_key,
        )
        git = GitOperations(workspace, credentials)
        try:
            git.pull_rebase(remote=remote)
        except Exception as e:
            logger.warning(
                "Workspace sync failed for %s; recreating workspace from fork: %s",
                ticket_key,
                e,
            )
            return await _recreate_workspace_from_fork(
                ticket_key=ticket_key,
                current_repo=current_repo,
                branch_name=branch_name,
                fork_owner=fork_owner,
                fork_repo=fork_repo,
                state=state,
                stale_workspace_path=workspace_path,
            )
        return workspace_path, git

    # Workspace is missing — recreate from fork branch.
    return await _recreate_workspace_from_fork(
        ticket_key=ticket_key,
        current_repo=current_repo,
        branch_name=branch_name,
        fork_owner=fork_owner,
        fork_repo=fork_repo,
        state=state,
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
    state = {**state, **repository_compatibility_update(state)}
    ticket_key = state["ticket_key"]
    current_repo = state.get("current_repository")
    tasks_by_repo = state.get("tasks_by_repo", {})
    repos_to_process = list(state.get("repos_to_process", []))

    # Determine which repo to set up
    if not current_repo:
        # Prefer normalized repository state, then task assignments, then the
        # Jira event labels used by taskless declarative workflows.
        repos = repos_to_process or list(tasks_by_repo.keys())
        if not repos:
            payload = state.get("context", {}).get("payload", {})
            labels = payload.get("issue", {}).get("fields", {}).get("labels", [])
            repos = list(
                dict.fromkeys(
                    label[5:]
                    for label in labels
                    if isinstance(label, str) and label.startswith("repo:") and "/" in label[5:]
                )
            )
        if not repos:
            logger.error(f"No repositories found for {ticket_key}")
            return {
                **state,
                "last_error": "No repositories to process",
                "current_node": "setup_workspace",
            }
        current_repo = repos[0]
        repos_to_process = repos

    state = {
        **state,
        **repository_compatibility_update(state, current=current_repo),
    }
    repos_to_process = list(state.get("repos_to_process", []))

    # Validate repository name
    if current_repo == "unknown":
        logger.error(
            f"Invalid repository name '{current_repo}' for {ticket_key}. "
            "Repository must be in 'owner/repo' format."
        )
        return {
            **state,
            "last_error": f"Invalid repository '{current_repo}'. Tasks must specify a valid 'owner/repo' format.",
            "current_node": "setup_workspace",
        }

    # Resolve current_repo (which may be a repos.yaml alias, not "owner/repo")
    # to its canonical namespace before cloning. Downstream PR-state keys and
    # webhook lookups are both keyed off this same value, so cloning under the
    # raw alias would silently desync CI/review/merge event matching later.
    raw_current_repo = current_repo
    try:
        repo_ref, adapter = get_adapter(current_repo)
    except (NotFoundError, ProviderConfigError) as exc:
        logger.error(f"Cannot resolve repository '{current_repo}' for {ticket_key}: {exc}")
        return {
            **state,
            "last_error": f"Invalid repository '{current_repo}': {exc}",
            "current_node": "setup_workspace",
        }
    current_repo = repo_ref.namespace

    # tasks_by_repo and repos_to_process were built (by task_router/plan_bug_fix/
    # task_takeover_planning) using the same raw identifier as current_repo. Once
    # that identifier is canonicalized above, both must be rewritten in lockstep
    # or implementation's tasks_by_repo lookup comes back empty and
    # route_after_pr's repos_to_process/repos_completed matching never
    # converges (the alias entry can never be marked completed).
    repos_to_process = state.get("repos_to_process") or repos_to_process
    if raw_current_repo != current_repo:
        if raw_current_repo in tasks_by_repo:
            tasks_by_repo = dict(tasks_by_repo)
            alias_tasks = tasks_by_repo.pop(raw_current_repo)
            tasks_by_repo[current_repo] = tasks_by_repo.get(current_repo, []) + alias_tasks
        if repos_to_process:
            repos_to_process = [
                current_repo if r == raw_current_repo else r for r in repos_to_process
            ]

    if "/" not in current_repo:
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
        task_keys = state.get("task_keys") or []
        if task_keys:
            await transition_tasks_to_in_progress(jira_client, task_keys)

        # Transition epic tickets to In Progress if present
        epic_keys = state.get("epic_keys") or []
        if epic_keys:
            await transition_tasks_to_in_progress(jira_client, epic_keys)

        # Transition parent Epic if present
        try:
            feature_issue = await jira_client.get_issue(ticket_key)
            if feature_issue.parent_key:
                await transition_tasks_to_in_progress(jira_client, [feature_issue.parent_key])
        except Exception as e:
            logger.warning(f"Failed to fetch issue {ticket_key} or transition its parent Epic: {e}")
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
        git = GitOperations(workspace, await adapter.get_git_credentials(repo_ref))

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
        try:
            default_branch = await adapter.resolve_default_branch(repo_ref)
            logger.info(f"Detected default branch for {current_repo}: {default_branch}")
        except Exception as exc:
            logger.warning(f"Could not detect default branch for {current_repo}: {exc}")

        write_target = await adapter.ensure_write_target(repo_ref)
        fork_owner = write_target.fork_owner or ""
        fork_repo_name = write_target.fork_repo or ""

        # Set up feature branch. Direct-mode repos (write_target.push_remote_name
        # == "origin") have no fork identity — work directly off origin instead
        # of building an invalid fork remote URL from empty owner/repo.
        use_fork = bool(fork_owner and fork_repo_name)
        push_remote = "fork" if use_fork else "origin"
        if use_fork:
            git.add_fork_remote(fork_owner, fork_repo_name)
        branch_exists_remotely = git.remote_branch_exists(workspace.branch_name, remote=push_remote)
        if branch_exists_remotely:
            logger.info(
                f"Branch '{workspace.branch_name}' exists on {push_remote} — checking it out"
            )
            git.checkout_branch(workspace.branch_name, remote=push_remote)
        else:
            git.create_branch(default_branch)
            # The next graph node may run on a worker that cannot see this
            # local workspace.  Publish the new branch before checkpointing so
            # that worker can recreate it from the fork (or origin, in direct
            # mode) on the first attempt.
            await push_to_fork_with_retry(git, use_fork=use_fork)

        # Create .forge directory for task handoff
        forge_dir = workspace.path / ".forge"
        forge_dir.mkdir(exist_ok=True)
        (forge_dir / "history").mkdir(exist_ok=True)
        write_workspace_identity(
            workspace.path,
            ticket_key=ticket_key,
            repo_name=current_repo,
        )
        materialize_handoff(workspace.path, current_repo, state)

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

        updates: dict[str, Any] = {
            **state,
            "workspace_path": str(workspace.path),
            "current_repo": current_repo,
            "current_repository": current_repo,
            "tasks_by_repo": tasks_by_repo,
            "fork_owner": fork_owner,
            "fork_repo": fork_repo_name,
            "context": context,
            "current_node": "implementation",
            "last_error": None,
        }
        if repos_to_process is not None:
            updates["repos_to_process"] = repos_to_process
        return update_state_timestamp(updates)

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
