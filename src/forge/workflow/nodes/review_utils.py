"""Shared primitives for container-backed workflow reviews."""

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

from forge.sandbox.runner import ContainerConfig, ContainerResult, ContainerRunner
from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)


def _review_history_file(workspace_path: Path, task_key: str) -> Path:
    return workspace_path / ".forge" / "history" / f"{task_key}.json"


def collect_review_output(
    workspace_path: Path,
    task_key: str,
    stdout: str,
    stderr: str,
) -> str:
    """Read the final AI response from history, falling back to process logs.

    The container entrypoint logs lifecycle messages to stdout; it does not
    print the agent's final response there.  The response is persisted in the
    task history file instead.
    """
    history_file = _review_history_file(workspace_path, task_key)
    try:
        history = json.loads(history_file.read_text())
        responses: list[str] = []
        for message in history.get("messages", []):
            if message.get("role") not in {"ai", "assistant"}:
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                if content.strip():
                    responses.append(content.strip())
            elif isinstance(content, list):
                text_blocks = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                response = "\n".join(text for text in text_blocks if text.strip()).strip()
                if response:
                    responses.append(response)
        if responses:
            return responses[-1]
        logger.warning("Review history contains no AI text: %s", history_file)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Unable to read review response from %s: %s", history_file, exc)

    return "\n".join(part for part in (stdout, stderr) if part)


def collect_git_diff(git: GitOperations) -> str:
    """Return the most useful available diff for a review prompt."""
    for args in (("diff", "HEAD~1", "HEAD"), ("diff", "HEAD~1"), ("diff",), ("show", "HEAD")):
        try:
            result = git._run_git(*args, check=False)
            if result.returncode == 0 and result.stdout.strip():
                return cast(str, result.stdout)
        except Exception:
            logger.debug("Unable to collect review diff with git %s", " ".join(args))
    return "No changes detected or unable to retrieve git diff."


def parse_review_verdict(
    output: str,
    *,
    valid_verdicts: set[str],
    default_verdict: str = "tests_incomplete",
) -> tuple[str, str]:
    """Parse the common ``verdict:`` / ``feedback:`` review protocol."""
    verdict = default_verdict
    match = re.search(r"verdict:\s*`?([a-zA-Z_]+)", output, re.IGNORECASE)
    if match:
        candidate = match.group(1).strip().lower()
        if candidate in valid_verdicts:
            verdict = candidate
        else:
            logger.warning(
                "Unrecognized review verdict %r, defaulting to %s",
                candidate,
                default_verdict,
            )

    feedback_match = re.search(r"feedback:\s*(.*)", output, re.IGNORECASE | re.DOTALL)
    feedback = feedback_match.group(1).strip() if feedback_match else ""
    return verdict, feedback


def next_review_attempt(current_attempts: int, *, passed: bool) -> int:
    """Increment a bounded review counter only when the review did not pass."""
    return current_attempts if passed else current_attempts + 1


def review_attempts_exhausted(attempts: int, limit: int) -> bool:
    """Return whether a review should be skipped after its bounded attempts."""
    return attempts >= limit


async def run_review_container(
    runner: ContainerRunner,
    *,
    workspace_path: Path,
    task_summary: str,
    task_description: str,
    ticket_key: str,
    task_key: str,
    repo_name: str,
    config: ContainerConfig | None = None,
    previous_task_keys: list[str] | None = None,
) -> tuple[ContainerResult, str]:
    """Execute a review container and return its result and combined output."""
    # Never allow a failed retry to reuse an earlier attempt's verdict. The
    # container writes this file only after a successful agent invocation.
    history_file = _review_history_file(workspace_path, task_key)
    try:
        history_file.unlink(missing_ok=True)
    except OSError:
        logger.exception("Unable to clear stale review history: %s", history_file)
        raise

    kwargs: dict[str, Any] = {
        "workspace_path": workspace_path,
        "task_summary": task_summary,
        "task_description": task_description,
        "ticket_key": ticket_key,
        "task_key": task_key,
        "repo_name": repo_name,
    }
    if config is not None:
        kwargs["config"] = config
    if previous_task_keys is not None:
        kwargs["previous_task_keys"] = previous_task_keys
    result = await runner.run(**kwargs)
    output = collect_review_output(
        workspace_path,
        task_key,
        result.stdout,
        result.stderr,
    )
    return result, output
