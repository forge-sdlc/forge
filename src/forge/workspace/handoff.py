"""Persist the semantic task handoff across ephemeral workspaces."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HANDOFF_PATH = Path(".forge/handoff.md")
MAX_HANDOFF_BYTES = 64 * 1024


def capture_handoff(
    workspace_path: str | Path,
    repo: str,
    task_key: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Capture the current repository handoff as bounded, structured state.

    A missing handoff removes any saved value for the repository. This mirrors
    the workspace exactly and prevents deleted content from being resurrected
    after a later workspace recreation.
    """
    saved_handoffs = state.get("handoffs", {})
    if not isinstance(saved_handoffs, dict):
        logger.warning("Ignoring malformed handoff state while capturing %s", repo)
        saved_handoffs = {}
    handoffs = dict(saved_handoffs)
    handoff_path = Path(workspace_path) / _HANDOFF_PATH

    try:
        size = handoff_path.stat().st_size
    except FileNotFoundError:
        handoffs.pop(repo, None)
        return {**state, "handoffs": handoffs}
    except OSError as exc:
        logger.warning("Failed to inspect handoff for %s: %s", repo, exc)
        return state

    if handoff_path.is_symlink() or not handoff_path.is_file():
        logger.warning("Ignoring non-regular handoff for %s", repo)
        handoffs.pop(repo, None)
        return {**state, "handoffs": handoffs}
    if size > MAX_HANDOFF_BYTES:
        logger.warning(
            "Ignoring oversized handoff for %s (%d bytes; limit %d)",
            repo,
            size,
            MAX_HANDOFF_BYTES,
        )
        handoffs.pop(repo, None)
        return {**state, "handoffs": handoffs}

    try:
        content = handoff_path.read_text()
    except (OSError, UnicodeError) as exc:
        logger.warning("Failed to capture handoff for %s: %s", repo, exc)
        return state

    handoffs[repo] = {
        "content": content,
        "task_key": task_key,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    return {**state, "handoffs": handoffs}


def materialize_handoff(
    workspace_path: str | Path,
    repo: str,
    state: dict[str, Any],
) -> None:
    """Materialize the saved repository handoff at its fixed workspace path."""
    handoffs = state.get("handoffs", {})
    if not isinstance(handoffs, dict):
        logger.warning("Ignoring malformed handoff state for %s", repo)
        return
    handoff = handoffs.get(repo)
    if not isinstance(handoff, dict):
        return

    content = handoff.get("content")
    try:
        content_size = len(content.encode()) if isinstance(content, str) else -1
    except UnicodeError:
        content_size = -1
    if content_size < 0 or content_size > MAX_HANDOFF_BYTES:
        logger.warning("Ignoring invalid saved handoff for %s", repo)
        return

    handoff_path = Path(workspace_path) / _HANDOFF_PATH
    temporary_path = handoff_path.with_suffix(".md.tmp")
    try:
        forge_dir = handoff_path.parent
        if forge_dir.is_symlink():
            logger.warning("Refusing to materialize handoff through symlink for %s", repo)
            return
        forge_dir.mkdir(parents=True, exist_ok=True)
        temporary_path.unlink(missing_ok=True)
        temporary_path.write_text(content)
        temporary_path.replace(handoff_path)
    except (OSError, UnicodeError) as exc:
        logger.warning("Failed to materialize handoff for %s: %s", repo, exc)
        temporary_path.unlink(missing_ok=True)
