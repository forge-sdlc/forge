"""Langfuse tracing integration for LLM observability."""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from forge.config import get_settings

logger = logging.getLogger(__name__)

# Flag to track if env vars are set
_env_configured = False


def _ensure_langfuse_env() -> bool:
    """Ensure Langfuse environment variables are set.

    Returns:
        True if Langfuse is enabled and configured.
    """
    global _env_configured

    settings = get_settings()
    if not settings.langfuse_enabled:
        return False

    if not _env_configured:
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        os.environ.setdefault(
            "LANGFUSE_SECRET_KEY", settings.langfuse_secret_key.get_secret_value()
        )
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
        _env_configured = True

    return True


def get_langfuse_handler(
    session_id: str | None = None,  # noqa: ARG001
    user_id: str | None = None,  # noqa: ARG001
    tags: list[str] | None = None,  # noqa: ARG001
) -> Any | None:
    """Get a Langfuse callback handler for LangChain/LangGraph.

    Creates a new handler instance. Note: In Langfuse v3+, session_id,
    user_id, and tags must be set via propagate_attributes() context manager
    around the actual invocation.

    Args:
        session_id: Session ID to group traces (e.g., ticket key).
        user_id: Optional user ID for attribution.
        tags: Optional tags for filtering.

    Returns:
        CallbackHandler instance or None if disabled.
    """
    if not _ensure_langfuse_env():
        return None

    try:
        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler()
        if session_id:
            logger.debug(f"Langfuse handler created for session: {session_id}")
        return handler

    except ImportError:
        logger.warning("langfuse package not installed. Install with: pip install langfuse")
        return None
    except Exception as e:
        logger.error(f"Failed to initialize Langfuse handler: {e}")
        return None


class AsyncLangfuseContext:
    """Async context manager for Langfuse attribute propagation.

    Wraps the sync propagate_attributes context manager for use in async code,
    suppressing OpenTelemetry context detach errors that occur when async
    contexts cross coroutine boundaries.
    """

    def __init__(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.tags = tags
        self.metadata = metadata
        self._ctx = None

    async def __aenter__(self):
        if not _ensure_langfuse_env():
            return self

        try:
            from langfuse import propagate_attributes

            self._ctx = propagate_attributes(
                session_id=self.session_id,
                user_id=self.user_id,
                tags=self.tags,
                metadata=self.metadata,
            )
            self._ctx.__enter__()
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"Langfuse context setup: {e}")

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._ctx is None:
            return False

        # Suppress OpenTelemetry context errors during cleanup
        otel_logger = logging.getLogger("opentelemetry.context")
        original_level = otel_logger.level
        otel_logger.setLevel(logging.CRITICAL)
        try:
            self._ctx.__exit__(None, None, None)
        except ValueError:
            # Ignore "created in a different Context" errors
            pass
        except Exception:
            pass
        finally:
            otel_logger.setLevel(original_level)

        return False  # Don't suppress exceptions from the body


def get_langfuse_context(
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AsyncLangfuseContext:
    """Get an async context manager for Langfuse attribute propagation.

    Use this to wrap agent invocations to set session_id, user_id, etc.

    Args:
        session_id: Session ID to group traces (e.g., ticket key).
        user_id: Optional user ID for attribution.
        tags: Optional tags for filtering.
        metadata: Optional metadata dict.

    Returns:
        AsyncLangfuseContext instance.
    """
    return AsyncLangfuseContext(
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        metadata=metadata,
    )


def get_langfuse_config(
    trace_name: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get a LangChain config dict with Langfuse callback.

    This can be passed directly to agent.invoke() or chain.invoke().
    Note: In Langfuse v3+, session_id/user_id/tags are stored in the
    returned dict under '_langfuse_context' for the caller to use
    with propagate_attributes().

    Args:
        trace_name: Optional name for the trace.
        user_id: Optional user ID for the trace.
        session_id: Optional session ID to group traces (e.g., ticket key).
        tags: Optional list of tags.
        metadata: Optional additional metadata.

    Returns:
        Config dict with callbacks and metadata, or empty dict if disabled.
    """
    handler = get_langfuse_handler()
    if handler is None:
        return {}

    config: dict[str, Any] = {"callbacks": [handler]}

    # Build metadata for trace name and additional data
    langfuse_metadata: dict[str, Any] = {}
    if trace_name:
        langfuse_metadata["langfuse_trace_name"] = trace_name
    if metadata:
        langfuse_metadata.update(metadata)

    if langfuse_metadata:
        config["metadata"] = langfuse_metadata

    # Store context params for caller to use with propagate_attributes
    config["_langfuse_context"] = {
        "session_id": session_id,
        "user_id": user_id,
        "tags": tags,
        "metadata": metadata,
    }

    return config


@contextmanager
def trace_llm_call(
    name: str,
    input_data: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Context manager for tracing LLM calls with Langfuse.

    Note: For LangChain/Deep Agents, use get_langfuse_config() instead
    and pass the callbacks via the config parameter.

    This context manager is kept for backwards compatibility with
    direct Anthropic API calls.

    Args:
        name: Name of the LLM operation.
        input_data: Input data for the LLM call.
        metadata: Optional additional metadata.

    Yields:
        Dict to capture output data (modify in place).
    """
    import os

    settings = get_settings()
    output_data: dict[str, Any] = {}

    if not settings.langfuse_enabled:
        yield output_data
        return

    try:
        from langfuse import Langfuse

        # Langfuse v4+ reads config from environment variables
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        os.environ.setdefault(
            "LANGFUSE_SECRET_KEY", settings.langfuse_secret_key.get_secret_value()
        )
        os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)

        langfuse = Langfuse()

        trace = langfuse.trace(name=name, input=input_data, metadata=metadata)
        generation = trace.generation(name=f"{name}_generation", input=input_data)

        yield output_data

        generation.end(output=output_data.get("output"))
        langfuse.flush()

    except ImportError:
        logger.debug("Langfuse not installed, skipping tracing")
        yield output_data
    except Exception as e:
        logger.warning(f"Langfuse tracing error: {e}")
        yield output_data


async def shutdown_langfuse() -> None:
    """Shutdown the Langfuse client and flush pending traces."""
    global _langfuse_handler

    if _langfuse_handler is not None:
        try:
            from langfuse import get_client

            client = get_client()
            client.flush()
            logger.info("Langfuse traces flushed")
        except Exception as e:
            logger.warning(f"Error flushing Langfuse: {e}")
        finally:
            _langfuse_handler = None
