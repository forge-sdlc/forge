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
    handoffs = dict(state.get("handoffs", {}))
    handoff_path = Path(workspace_path) / _HANDOFF_PATH

    if not handoff_path.is_file():
        handoffs.pop(repo, None)
        return {**state, "handoffs": handoffs}

    size = handoff_path.stat().st_size
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
    handoff = state.get("handoffs", {}).get(repo)
    if not handoff:
        return

    content = handoff.get("content")
    if not isinstance(content, str) or len(content.encode()) > MAX_HANDOFF_BYTES:
        logger.warning("Ignoring invalid saved handoff for %s", repo)
        return

    handoff_path = Path(workspace_path) / _HANDOFF_PATH
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = handoff_path.with_suffix(".md.tmp")
    try:
        temporary_path.write_text(content)
        temporary_path.replace(handoff_path)
    except (OSError, UnicodeError) as exc:
        logger.warning("Failed to materialize handoff for %s: %s", repo, exc)
        temporary_path.unlink(missing_ok=True)
