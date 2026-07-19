"""Retry logic with exponential backoff for transient failures."""

import asyncio
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """Configuration for retry behavior.

    Attributes:
        max_attempts: Maximum number of retry attempts.
        initial_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        exponential_base: Base for exponential backoff.
        jitter: Add random jitter to delays.
        retryable_exceptions: Exception types to retry on.
    """

    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
    )


def calculate_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """Calculate delay for a retry attempt.

    Uses exponential backoff with optional jitter.

    Args:
        attempt: Current attempt number (0-indexed).
        config: Retry configuration.

    Returns:
        Delay in seconds.
    """
    delay = config.initial_delay * (config.exponential_base**attempt)
    delay = min(delay, config.max_delay)

    if config.jitter:
        # Add up to 25% random jitter
        jitter = delay * random.uniform(0, 0.25)
        delay += jitter

    return delay


async def retry_async(
    func: Callable[..., T],
    *args: Any,
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> T:
    """Execute an async function with retry logic.

    Args:
        func: Async function to execute.
        *args: Positional arguments for func.
        config: Retry configuration.
        **kwargs: Keyword arguments for func.

    Returns:
        Result from successful function call.

    Raises:
        Exception: Last exception if all retries exhausted.
    """
    config = config or RetryConfig()
    last_exception: Exception | None = None

    for attempt in range(config.max_attempts):
        try:
            return await func(*args, **kwargs)
        except config.retryable_exceptions as e:
            last_exception = e
            if attempt < config.max_attempts - 1:
                delay = calculate_delay(attempt, config)
                logger.warning(
                    f"Retry attempt {attempt + 1}/{config.max_attempts} "
                    f"for {func.__name__}: {e}. "
                    f"Waiting {delay:.2f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"All {config.max_attempts} retry attempts exhausted for {func.__name__}: {e}"
                )

    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected retry state")


def with_retry(
    config: RetryConfig | None = None,
    retryable_exceptions: Sequence[type[Exception]] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for adding retry logic to async functions.

    Args:
        config: Retry configuration.
        retryable_exceptions: Override exception types to retry.

    Returns:
        Decorated function.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            retry_config = config or RetryConfig()
            if retryable_exceptions:
                retry_config = RetryConfig(
                    max_attempts=retry_config.max_attempts,
                    initial_delay=retry_config.initial_delay,
                    max_delay=retry_config.max_delay,
                    exponential_base=retry_config.exponential_base,
                    jitter=retry_config.jitter,
                    retryable_exceptions=tuple(retryable_exceptions),
                )
            return await retry_async(func, *args, config=retry_config, **kwargs)

        return wrapper

    return decorator


class RetryableError(Exception):
    """Exception that signals a retryable error.

    Use this to wrap non-retryable exceptions that should be retried.
    """

    def __init__(self, message: str, original: Exception | None = None):
        """Initialize retryable error.

        Args:
            message: Error message.
            original: Original exception.
        """
        super().__init__(message)
        self.original = original


# Pre-configured retry configs for common scenarios
JIRA_RETRY_CONFIG = RetryConfig(
    max_attempts=3,
    initial_delay=1.0,
    max_delay=30.0,
    retryable_exceptions=(
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        RetryableError,
    ),
)

GITHUB_RETRY_CONFIG = RetryConfig(
    max_attempts=3,
    initial_delay=2.0,
    max_delay=60.0,
    retryable_exceptions=(
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        RetryableError,
    ),
)
