"""Checkpointer for LangGraph workflow state persistence.

Uses Redis for checkpoint storage to enable workflow pause/resume
across process restarts. Shares the Redis instance used by the queue.

## Checkpoint Recovery Behavior

When a workflow is interrupted (process crash, restart, etc.), the checkpoint
system enables transparent recovery:

1. **On workflow invocation**: LangGraph checks for existing checkpoint
   with the given thread_id (ticket key).

2. **If checkpoint exists**: Workflow resumes from the last saved state,
   including all context, current_node, retry_count, and other state fields.
   The workflow continues from the exact node where it was interrupted.

3. **If no checkpoint**: Fresh workflow execution begins.

4. **Checkpoint writes**: Each node completion automatically saves state.
   Checkpoints are transactional and atomic.

5. **Pause gates**: When a workflow hits a pause gate (human approval),
   the checkpoint persists the is_paused=True state. On resume, the gate
   checks this flag and continues appropriately.

## Thread ID Convention

Thread IDs are Jira ticket keys (e.g., "AISOS-123"). This provides:
- Natural 1:1 mapping of ticket to workflow instance
- Easy inspection and debugging
- Clear correlation in logs and monitoring

## Checkpoint Cleanup

Use `clear_checkpoint(thread_id)` to reset a workflow. This is necessary when:
- A workflow needs to restart from scratch
- Debugging workflow state issues
- Cleaning up after failed test runs
"""

import logging

import redis.asyncio as redis
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from forge.config import get_settings

logger = logging.getLogger(__name__)

# Global instances
_checkpointer: AsyncRedisSaver | None = None
_checkpointer_context = None
_redis_pool: redis.ConnectionPool | None = None


async def get_checkpointer() -> AsyncRedisSaver:
    """Get a configured Redis checkpointer instance.

    Uses AsyncRedisSaver from langgraph-checkpoint-redis for proper
    LangGraph checkpoint API compatibility. Shares the Redis connection
    with the queue system.

    Returns:
        Configured AsyncRedisSaver instance.
    """
    global _checkpointer, _checkpointer_context

    if _checkpointer is None:
        settings = get_settings()

        # from_conn_string returns an async context manager
        _checkpointer_context = AsyncRedisSaver.from_conn_string(settings.redis_url)
        # Manually enter the context manager for long-running use
        _checkpointer = await _checkpointer_context.__aenter__()
        # Initialize Redis indices for checkpointing
        await _checkpointer.asetup()

    return _checkpointer


async def close_checkpointer() -> None:
    """Close the checkpointer connection."""
    global _checkpointer, _checkpointer_context

    if _checkpointer is not None and _checkpointer_context is not None:
        await _checkpointer_context.__aexit__(None, None, None)
        _checkpointer = None
        _checkpointer_context = None


async def get_redis_client() -> redis.Redis:
    """Get a Redis client from the connection pool.

    This is used by the queue consumer and other components that need
    direct Redis access for message queuing.

    Returns:
        Async Redis client instance.
    """
    global _redis_pool

    settings = get_settings()

    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )

    return redis.Redis(connection_pool=_redis_pool)


async def close_redis_pool() -> None:
    """Close the Redis connection pool."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None


async def clear_checkpoint(thread_id: str) -> bool:
    """Clear checkpoint state for a specific thread/ticket.

    Args:
        thread_id: The thread ID (typically the ticket key like AISOS-104).

    Returns:
        True if checkpoint was cleared, False if not found.
    """
    checkpointer = await get_checkpointer()

    # Check if checkpoint exists first
    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint = await checkpointer.aget(config)
        if checkpoint is None:
            return False
    except Exception:
        return False

    # Use the built-in delete method
    await checkpointer.adelete_thread(thread_id)
    logger.info(f"Cleared checkpoint for {thread_id}")
    return True


async def get_checkpoint_state(thread_id: str) -> dict | None:
    """Get the current checkpoint state for a thread.

    Useful for debugging and verification.

    Args:
        thread_id: The thread ID (typically the ticket key).

    Returns:
        Checkpoint state dict or None if no checkpoint exists.
    """
    from forge.models.workflow import TicketType
    from forge.workflow.registry import create_default_router

    checkpointer = await get_checkpointer()

    # Create a minimal Feature workflow to retrieve state
    # (This is just for state retrieval - actual type doesn't matter)
    router = create_default_router()
    workflow_instance = router.resolve(
        ticket_type=TicketType.FEATURE,
        labels=[],
        event={},
    )

    if workflow_instance is None:
        logger.error("Failed to resolve workflow for state retrieval")
        return None

    graph = workflow_instance.build_graph()
    compiled_workflow = graph.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": thread_id}}
    state = await compiled_workflow.aget_state(config)

    if state and state.values:
        return dict(state.values)
    return None


async def verify_checkpoint_recovery(thread_id: str) -> dict:
    """Verify checkpoint recovery for a thread.

    Tests that checkpoint can be read and contains expected fields.
    Used for testing and validation.

    Args:
        thread_id: The thread ID to verify.

    Returns:
        Dict with verification results:
        - has_checkpoint: bool
        - current_node: str or None
        - is_paused: bool or None
        - retry_count: int or None
        - context_keys: list of keys in context
        - last_updated: str or None
    """
    state = await get_checkpoint_state(thread_id)

    if state is None:
        logger.info(f"No checkpoint found for {thread_id}")
        return {
            "has_checkpoint": False,
            "current_node": None,
            "is_paused": None,
            "retry_count": None,
            "context_keys": [],
            "last_updated": None,
        }

    result = {
        "has_checkpoint": True,
        "current_node": state.get("current_node"),
        "is_paused": state.get("is_paused"),
        "retry_count": state.get("retry_count", 0),
        "context_keys": list(state.get("context", {}).keys()),
        "last_updated": state.get("updated_at"),
    }

    logger.info(
        f"Checkpoint verified for {thread_id}: "
        f"node={result['current_node']}, "
        f"paused={result['is_paused']}, "
        f"retries={result['retry_count']}"
    )

    return result


_PR_INDEX_KEY = "forge:state:pr_index"


async def set_pr_ticket_index(pr_url: str, ticket_key: str) -> None:
    """Associate a PR URL with a Jira ticket key in the state PR index.

    Stored as a Redis HASH field so all PR-to-ticket mappings live under a
    single managed key rather than scattered per-PR string keys. No TTL —
    PR URL to ticket key mappings are permanent.

    Args:
        pr_url: The full GitHub PR URL (e.g. https://github.com/org/repo/pull/42).
        ticket_key: The Jira ticket key (e.g. PROJ-123).
    """
    redis_client = await get_redis_client()
    await redis_client.hset(_PR_INDEX_KEY, pr_url, ticket_key)
    logger.debug(f"PR index: {pr_url} → {ticket_key}")


async def get_ticket_from_pr_index(pr_url: str) -> str | None:
    """Look up the Jira ticket key for a PR URL from the state PR index.

    Args:
        pr_url: The full GitHub PR URL to look up.

    Returns:
        The associated ticket key, or None if not found.
    """
    redis_client = await get_redis_client()
    return await redis_client.hget(_PR_INDEX_KEY, pr_url) or None


async def list_checkpoints(limit: int = 100) -> list[dict]:
    """List all checkpoint thread IDs.

    Args:
        limit: Maximum number of checkpoints to return.

    Returns:
        List of checkpoint summaries with thread_id and metadata.
    """
    redis_client = await get_redis_client()

    # Find unique thread IDs by scanning checkpoint keys
    # Keys are stored with prefix "checkpoint:" followed by thread_id
    thread_ids: set[str] = set()

    async for key in redis_client.scan_iter(match="checkpoint:*"):
        # Keys are like: checkpoint:{thread_id}:{...}
        parts = key.split(":")
        if len(parts) >= 2:
            thread_ids.add(parts[1])
            if len(thread_ids) >= limit:
                break

    return [{"thread_id": tid} for tid in sorted(thread_ids)]
