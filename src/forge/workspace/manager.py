"""Ephemeral workspace manager for code execution."""

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    """Represents an ephemeral workspace for code execution."""

    path: Path
    repo_name: str
    branch_name: str
    ticket_key: str
    is_active: bool = True

    def __str__(self) -> str:
        return f"Workspace({self.repo_name}:{self.branch_name} at {self.path})"


class WorkspaceManager:
    """Manages ephemeral workspaces for code execution.

    Creates temporary directories for cloning repositories,
    making changes, and cleaning up after PR creation.
    """

    def __init__(self, base_dir: str | None = None):
        """Initialize the workspace manager.

        Args:
            base_dir: Base directory for workspaces. Uses system temp if None.
        """
        self.base_dir = Path(base_dir) if base_dir else None
        self._workspaces: dict[str, Workspace] = {}

    def create_workspace(
        self,
        repo_name: str,
        ticket_key: str,
        branch_name: str | None = None,
    ) -> Workspace:
        """Create a new ephemeral workspace.

        Args:
            repo_name: Name of the repository (e.g., "org/repo").
            ticket_key: Jira ticket key for tracking.
            branch_name: Optional branch name. Defaults to ticket_key.

        Returns:
            Created Workspace instance.
        """
        branch = branch_name or f"forge/{ticket_key.lower()}"

        # Create workspace in temp directory
        if self.base_dir:
            workspace_dir = self.base_dir / f"forge-{ticket_key}-{repo_name.replace('/', '-')}"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            path = workspace_dir
        else:
            path = Path(tempfile.mkdtemp(prefix=f"forge-{ticket_key}-"))

        workspace = Workspace(
            path=path,
            repo_name=repo_name,
            branch_name=branch,
            ticket_key=ticket_key,
        )

        workspace_id = f"{ticket_key}:{repo_name}"
        self._workspaces[workspace_id] = workspace

        logger.info(f"Created workspace: {workspace}")
        return workspace

    def get_workspace(
        self,
        ticket_key: str,
        repo_name: str,
    ) -> Workspace | None:
        """Get an existing workspace.

        Args:
            ticket_key: Jira ticket key.
            repo_name: Repository name.

        Returns:
            Workspace if found and active, None otherwise.
        """
        workspace_id = f"{ticket_key}:{repo_name}"
        workspace = self._workspaces.get(workspace_id)

        if workspace and workspace.is_active:
            return workspace
        return None

    def destroy_workspace(self, workspace: Workspace) -> None:
        """Destroy a workspace and clean up files.

        Uses podman unshare rm -rf to handle root-owned files written by
        containers. Falls back to shutil.rmtree when podman is unavailable
        or the unshare command fails.
        """
        workspace_id = f"{workspace.ticket_key}:{workspace.repo_name}"

        if workspace.path.exists():
            podman = shutil.which("podman")
            if podman:
                try:
                    result = subprocess.run(
                        [podman, "unshare", "rm", "-rf", str(workspace.path)],
                        check=False,
                    )
                except OSError as exc:
                    logger.warning(
                        "podman unshare failed for %s, falling back to shutil: %s",
                        workspace.path,
                        exc,
                    )
                    shutil.rmtree(workspace.path)
                else:
                    if result.returncode != 0:
                        logger.warning(
                            "podman unshare failed for %s, falling back to shutil",
                            workspace.path,
                        )
                        shutil.rmtree(workspace.path)
            else:
                shutil.rmtree(workspace.path)
            logger.info(f"Destroyed workspace: {workspace}")

        workspace.is_active = False
        if workspace_id in self._workspaces:
            del self._workspaces[workspace_id]

    def destroy_all_for_ticket(self, ticket_key: str) -> int:
        """Destroy all workspaces for a ticket.

        Args:
            ticket_key: Jira ticket key.

        Returns:
            Number of workspaces destroyed.
        """
        to_destroy = [
            ws for ws_id, ws in self._workspaces.items() if ws_id.startswith(f"{ticket_key}:")
        ]

        for workspace in to_destroy:
            self.destroy_workspace(workspace)

        return len(to_destroy)

    def cleanup_stale_workspaces(self, max_age_hours: int = 24) -> int:
        """Clean up workspaces older than max_age_hours.

        Args:
            max_age_hours: Maximum age in hours before cleanup.

        Returns:
            Number of workspaces cleaned up.
        """
        import time

        now = time.time()
        max_age_seconds = max_age_hours * 3600
        cleaned = 0

        for workspace in list(self._workspaces.values()):
            if not workspace.path.exists():
                workspace.is_active = False
                continue

            try:
                mtime = workspace.path.stat().st_mtime
                if now - mtime > max_age_seconds:
                    self.destroy_workspace(workspace)
                    cleaned += 1
            except Exception as e:
                logger.warning(f"Error checking workspace age: {e}")

        return cleaned

    def get_active_workspaces(self) -> list[Workspace]:
        """Get all active workspaces.

        Returns:
            List of active Workspace instances.
        """
        return [ws for ws in self._workspaces.values() if ws.is_active and ws.path.exists()]

    def workspace_exists(self, ticket_key: str, repo_name: str) -> bool:
        """Check if a workspace exists and is active.

        Args:
            ticket_key: Jira ticket key.
            repo_name: Repository name.

        Returns:
            True if workspace exists and is active.
        """
        return self.get_workspace(ticket_key, repo_name) is not None
