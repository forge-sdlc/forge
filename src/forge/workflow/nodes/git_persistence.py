"""Durable Git persistence helpers for workflow handoffs."""

import asyncio
import logging
from enum import StrEnum

from forge.workspace.git_ops import GitOperations

logger = logging.getLogger(__name__)


class PushFailureKind(StrEnum):
    """Actionable categories for a failed Git push."""

    TRANSIENT = "transient"
    AUTH = "auth"
    NON_FAST_FORWARD = "non_fast_forward"
    PERMANENT = "permanent"


class PushPersistenceError(RuntimeError):
    """Raised when a branch cannot be persisted after the allowed attempts."""

    def __init__(self, message: str, kind: PushFailureKind):
        super().__init__(message)
        self.kind = kind


def classify_push_failure(error: Exception) -> PushFailureKind:
    """Classify Git's text error until GitError exposes structured metadata."""
    message = str(error).lower()
    if any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "could not resolve host",
            "connection reset",
            "connection refused",
            "remote end hung up",
            "network is unreachable",
            "rate limit",
            "http 429",
            "http 502",
            "http 503",
            "http 504",
        )
    ):
        return PushFailureKind.TRANSIENT
    if any(
        marker in message
        for marker in (
            "authentication failed",
            "permission denied",
            "could not read username",
            "repository not found",
            "http 401",
            "http 403",
        )
    ):
        return PushFailureKind.AUTH
    if any(
        marker in message
        for marker in (
            "non-fast-forward",
            "fetch first",
            "[rejected]",
            "failed to push some refs",
        )
    ):
        return PushFailureKind.NON_FAST_FORWARD
    return PushFailureKind.PERMANENT


async def push_to_fork_with_retry(
    git: GitOperations,
    *,
    use_fork: bool = True,
    max_attempts: int = 3,
    initial_delay_seconds: float = 1.0,
) -> None:
    """Push a workflow branch, retrying only failures known to be transient.

    Args:
        git: Git operations bound to the workspace.
        use_fork: Push to the 'fork' remote (default, fork mode). When False
            (direct mode — no fork identity), pushes to 'origin' instead.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            if use_fork:
                git.push_to_fork()
            else:
                git.push(force=False, check_conflicts=False)
            return
        except Exception as exc:
            kind = classify_push_failure(exc)
            if kind != PushFailureKind.TRANSIENT or attempt >= max_attempts:
                raise PushPersistenceError(str(exc), kind) from exc
            delay = initial_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "Transient fork push failure (%s/%s); retrying in %.1fs: %s",
                attempt,
                max_attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


def use_fork_remote(state: dict) -> bool:
    """Whether this workflow's write target is a fork (vs. direct-to-origin).

    Fork identity (fork_owner/fork_repo) is only populated in state for
    change_request_mode == "fork" repos; direct-mode repos leave both empty.
    """
    return bool(state.get("fork_owner") and state.get("fork_repo"))


def build_persistence_error_state(
    state: dict,
    error: PushPersistenceError,
    *,
    retry_node: str,
    escalation_node: str | None = None,
    max_workflow_attempts: int = 3,
) -> dict:
    """Build consistent workflow state for an exhausted push operation."""
    previous = state.get("persistence_retry_count", 0)
    attempts = max_workflow_attempts if error.kind != PushFailureKind.TRANSIENT else previous + 1
    current_node = (
        escalation_node if escalation_node and attempts >= max_workflow_attempts else retry_node
    )
    return {
        **state,
        "last_error": str(error),
        "current_node": current_node,
        "persistence_retry_count": attempts,
    }
