"""Utilities for persisting .forge/ artifacts in workflow state across workspace recreations.

Harvest: after a container run, call harvest_forge_artifacts to read container-written
files from .forge/ into state keyed by repo name.

Restore: at workspace setup or recreation, call restore_forge_artifacts to write
those files back to .forge/ before the next container starts.
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def harvest_forge_artifacts(
    workspace_path: str | Path,
    repo: str,
    files: list[str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Read named files from .forge/ and merge their contents into state.

    Files that do not exist are silently skipped. Existing artifacts for
    other repos or other filenames in this repo are preserved.

    Args:
        workspace_path: Path to the workspace root (parent of .forge/).
        repo: Repository name e.g. "org/repo", used as the outer state key.
        files: Relative paths within .forge/ to harvest e.g. ["handoff.md"].
        state: Current workflow state dict.

    Returns:
        New state dict with forge_artifacts updated for this repo.
    """
    forge_dir = Path(workspace_path) / ".forge"
    all_artifacts: dict[str, dict[str, str]] = dict(state.get("forge_artifacts", {}))
    repo_artifacts: dict[str, str] = dict(all_artifacts.get(repo, {}))

    for filename in files:
        file_path = forge_dir / filename
        if file_path.exists():
            try:
                repo_artifacts[filename] = file_path.read_text()
                logger.debug(f"Harvested .forge/{filename} for {repo}")
            except Exception as e:
                logger.warning(f"Failed to harvest .forge/{filename} for {repo}: {e}")

    all_artifacts[repo] = repo_artifacts
    return {**state, "forge_artifacts": all_artifacts}


def restore_forge_artifacts(
    workspace_path: str | Path,
    repo: str,
    state: dict[str, Any],
) -> None:
    """Write all harvested artifacts for this repo back to .forge/.

    Creates parent directories as needed. No-op when no artifacts exist
    for this repo in state.

    Args:
        workspace_path: Path to the workspace root (parent of .forge/).
        repo: Repository name e.g. "org/repo".
        state: Current workflow state dict.
    """
    artifacts = state.get("forge_artifacts", {}).get(repo, {})
    if not artifacts:
        return

    forge_dir = Path(workspace_path) / ".forge"
    for filename, content in artifacts.items():
        file_path = forge_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            file_path.write_text(content)
            logger.debug(f"Restored .forge/{filename} for {repo}")
        except Exception as e:
            logger.warning(f"Failed to restore .forge/{filename} for {repo}: {e}")
