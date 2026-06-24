"""Git operations for workspace management."""

import logging
import subprocess
from pathlib import Path

from forge.config import get_settings
from forge.utils.redaction import redact_secrets
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


class GitOperations:
    """Git operations for cloning, branching, committing, and pushing."""

    def __init__(self, workspace: Workspace):
        """Initialize git operations for a workspace.

        Args:
            workspace: Workspace to operate on.
        """
        self.workspace = workspace
        self.settings = get_settings()

    @property
    def repo_path(self) -> Path:
        """Get the repository path."""
        return self.workspace.path

    def _run_git(
        self,
        *args: str,
        capture_output: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the workspace.

        Args:
            *args: Git command arguments.
            capture_output: Capture stdout/stderr.
            check: Raise on non-zero exit.

        Returns:
            CompletedProcess result.
        """
        cmd = ["git", *args]
        safe_cmd = redact_secrets(" ".join(cmd))
        logger.debug(f"Running: {safe_cmd} in {self.repo_path}")

        result = subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=capture_output,
            text=True,
            check=False,
        )

        if check and result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            raise GitError(f"Git command failed: {safe_cmd}\n{error_msg}")

        return result

    def clone(
        self,
        repo_url: str | None = None,
        timeout: int = 600,
    ) -> None:
        """Clone the repository into the workspace.

        Args:
            repo_url: Repository URL. Constructs from settings if None.
            timeout: Timeout in seconds for the clone operation (default 600s).
        """
        if repo_url is None:
            token = self.settings.github_token.get_secret_value()
            repo_url = f"https://x-access-token:{token}@github.com/{self.workspace.repo_name}.git"

        # Build clone command (single-branch for faster clone)
        cmd = ["git", "clone", "--single-branch", repo_url, str(self.repo_path)]

        logger.info(f"Cloning {self.workspace.repo_name} to {self.repo_path} (timeout: {timeout}s)")
        logger.debug(f"Clone command: git clone --single-branch [REDACTED] {self.repo_path}")

        # Clone with timeout
        import time

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
            elapsed = time.time() - start_time
            logger.info(f"Clone completed for {self.workspace.repo_name} in {elapsed:.1f}s")
            if result.stderr:
                logger.debug(f"Clone stderr: {redact_secrets(result.stderr[:500])}")
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            logger.error(
                f"Clone timed out after {elapsed:.1f}s for {self.workspace.repo_name}. "
                f"Target path: {self.repo_path}"
            )
            raise GitError(f"Clone timed out after {timeout}s for {self.workspace.repo_name}")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""
            output = stderr or stdout or "Unknown error"
            logger.error(
                f"Clone failed for {self.workspace.repo_name}: {redact_secrets(output[:500])}"
            )
            raise GitError(
                f"Git clone failed for {self.workspace.repo_name} with exit code "
                f"{e.returncode}: {output}"
            )

    def pull_rebase(self, remote: str = "fork") -> None:
        """Fetch and rebase the current branch onto its remote counterpart.

        Keeps the local workspace aligned with the remote branch before
        making any changes, preventing non-fast-forward push failures when
        another worker or external push has moved the branch tip forward.

        Args:
            remote: Remote name to pull from (default: 'fork').
        """
        branch = self.workspace.branch_name
        logger.info(f"Syncing with {remote}/{branch} before implementing changes")
        self._run_git("fetch", remote)
        self._run_git("rebase", f"{remote}/{branch}")
        logger.info(f"Rebase onto {remote}/{branch} complete")

    def add_fork_remote(self, fork_owner: str, fork_repo: str) -> None:
        """Add the fork as a remote named 'fork'.

        Args:
            fork_owner: Owner of the fork repository.
            fork_repo: Name of the fork repository.
        """
        token = self.settings.github_token.get_secret_value()
        fork_url = f"https://x-access-token:{token}@github.com/{fork_owner}/{fork_repo}.git"

        # Check if remote already exists
        result = self._run_git("remote", check=False)
        if "fork" in result.stdout.split():
            # Update existing remote
            self._run_git("remote", "set-url", "fork", fork_url)
            logger.info(f"Updated fork remote to {fork_owner}/{fork_repo}")
        else:
            # Add new remote
            self._run_git("remote", "add", "fork", fork_url)
            logger.info(f"Added fork remote: {fork_owner}/{fork_repo}")

        # Fetch from fork
        self._run_git("fetch", "fork", check=False)

    def push_to_fork(self, force: bool = False) -> None:
        """Push the current branch to the fork remote.

        Args:
            force: Force push (use with caution).
        """
        args = ["push", "-u", "fork", self.workspace.branch_name]
        if force:
            args.insert(1, "--force")

        self._run_git(*args)
        logger.info(f"Pushed branch {self.workspace.branch_name} to fork")

    def create_branch(self, base_branch: str = "main") -> None:
        """Create and checkout a new branch.

        Args:
            base_branch: Base branch to branch from.
        """
        # Fetch latest
        self._run_git("fetch", "origin", base_branch)

        # Create and checkout branch
        self._run_git("checkout", "-b", self.workspace.branch_name, f"origin/{base_branch}")
        logger.info(f"Created branch {self.workspace.branch_name} from {base_branch}")

    def checkout_branch(self, branch_name: str | None = None, remote: str = "origin") -> None:
        """Checkout an existing branch, tracking the remote if needed.

        On a fresh clone the branch may only exist on the remote. This
        method tries a plain checkout first (works when the local branch
        already exists) and falls back to ``-b branch remote/branch`` so
        the remote tracking branch is set up correctly.

        Args:
            branch_name: Branch to checkout. Uses workspace branch if None.
            remote: Remote to track when creating the local branch (default: "origin").
        """
        branch = branch_name or self.workspace.branch_name

        # Try a plain checkout; if the branch doesn't exist locally yet,
        # create it tracking the specified remote.
        result = self._run_git("checkout", branch, check=False)
        if result.returncode != 0:
            self._run_git("checkout", "-b", branch, f"{remote}/{branch}")

        logger.info(f"Checked out branch {branch}")

    def stage_all(self) -> None:
        """Stage all changes."""
        self._run_git("add", "-A")

    def stage_files(self, *files: str) -> None:
        """Stage specific files.

        Args:
            *files: File paths to stage.
        """
        if files:
            self._run_git("add", *files)

    def commit(self, message: str, author_name: str = "Forge") -> bool:
        """Create a commit with the staged changes.

        Args:
            message: Commit message.
            author_name: Author name for the commit.

        Returns:
            True if commit was created, False if nothing to commit.
        """
        # Check if there are changes to commit
        result = self._run_git("status", "--porcelain", check=False)
        if not result.stdout.strip():
            logger.info("Nothing to commit")
            return False

        self._run_git(
            "commit",
            "-m",
            message,
            "--author",
            f"{author_name} <forge@noreply.anthropic.com>",
        )
        logger.info(f"Committed: {message[:50]}...")
        return True

    def remote_branch_exists(self, branch_name: str, remote: str = "origin") -> bool:
        """Check whether a branch exists on the given remote.

        Args:
            branch_name: Branch name to check.
            remote: Remote name to query (default: "origin").

        Returns:
            True if the branch exists on the remote.
        """
        result = self._run_git("ls-remote", "--heads", remote, branch_name, check=False)
        return bool(result.stdout.strip())

    def check_for_conflicts(self, target_branch: str = "main") -> tuple[bool, list[str]]:
        """Check if pushing would cause conflicts with remote.

        Fetches remote and checks if local and remote have diverged.

        Args:
            target_branch: Branch to check against (default: main).

        Returns:
            Tuple of (has_conflicts, conflicting_files).
            has_conflicts is True if remote has commits not in local.
        """
        # Fetch remote changes
        self._run_git("fetch", "origin", check=False)

        # Check if remote branch exists
        result = self._run_git(
            "ls-remote", "--heads", "origin", self.workspace.branch_name, check=False
        )

        if not result.stdout.strip():
            # Branch doesn't exist on remote, no conflicts
            return False, []

        # Check for divergence
        local_sha = self._run_git(
            "rev-parse", self.workspace.branch_name, check=False
        ).stdout.strip()

        remote_sha = self._run_git(
            "rev-parse", f"origin/{self.workspace.branch_name}", check=False
        ).stdout.strip()

        if local_sha == remote_sha:
            # No divergence
            return False, []

        # Check if remote has commits not in local (we need to merge/rebase)
        result = self._run_git(
            "rev-list",
            "--count",
            f"{self.workspace.branch_name}..origin/{self.workspace.branch_name}",
            check=False,
        )

        remote_ahead = int(result.stdout.strip() or "0")
        if remote_ahead == 0:
            # We're ahead but not diverged
            return False, []

        # Check for actual file conflicts with target branch
        conflicting_files: list[str] = []

        # Try a dry-run merge to detect conflicts
        result = self._run_git(
            "merge", "--no-commit", "--no-ff", f"origin/{target_branch}", check=False
        )

        if result.returncode != 0:
            # Merge would fail - get conflicting files
            status = self._run_git("status", "--porcelain", check=False)
            for line in status.stdout.split("\n"):
                if line.startswith("UU") or line.startswith("AA") or line.startswith("DD"):
                    conflicting_files.append(line[3:].strip())

            # Abort the merge attempt
            self._run_git("merge", "--abort", check=False)

        logger.warning(
            f"Branch {self.workspace.branch_name} has diverged from remote. "
            f"Remote is {remote_ahead} commits ahead. "
            f"Conflicting files: {conflicting_files or 'none detected'}"
        )

        return True, conflicting_files

    def push(self, force: bool = False, check_conflicts: bool = True) -> None:
        """Push the current branch to origin.

        Args:
            force: Force push (use with caution).
            check_conflicts: Check for conflicts before pushing (default: True).

        Raises:
            GitError: If conflicts detected and check_conflicts is True.
        """
        if check_conflicts and not force:
            has_conflicts, conflicting_files = self.check_for_conflicts()
            if has_conflicts:
                raise GitError(
                    f"Cannot push: branch has diverged from remote. "
                    f"Conflicting files: {conflicting_files}. "
                    "Use force=True to override or resolve conflicts first."
                )

        args = ["push", "-u", "origin", self.workspace.branch_name]
        if force:
            args.insert(1, "--force")

        self._run_git(*args)
        logger.info(f"Pushed branch {self.workspace.branch_name}")

    def get_current_sha(self) -> str:
        """Get the current commit SHA.

        Returns:
            Full commit SHA.
        """
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def get_diff_stats(self) -> dict[str, int]:
        """Get statistics about changes.

        Returns:
            Dict with files_changed, insertions, deletions.
        """
        result = self._run_git("diff", "--stat", "--cached", check=False)

        stats = {"files_changed": 0, "insertions": 0, "deletions": 0}

        for line in result.stdout.strip().split("\n"):
            if "files changed" in line or "file changed" in line:
                parts = line.split(",")
                for part in parts:
                    part = part.strip()
                    if "file" in part:
                        stats["files_changed"] = int(part.split()[0])
                    elif "insertion" in part:
                        stats["insertions"] = int(part.split()[0])
                    elif "deletion" in part:
                        stats["deletions"] = int(part.split()[0])

        return stats

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes.

        Returns:
            True if there are uncommitted changes.
        """
        result = self._run_git("status", "--porcelain", check=False)
        return bool(result.stdout.strip())

    def reset_hard(self) -> None:
        """Reset all changes (use with caution)."""
        self._run_git("reset", "--hard", "HEAD")
        self._run_git("clean", "-fd")
        logger.info("Reset workspace to HEAD")


class GitError(Exception):
    """Raised when a git operation fails."""

    def __init__(self, message: object):
        """Initialize with a redacted error message."""
        super().__init__(redact_secrets(message))
