"""Rate limiting utilities for external API calls."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting.

    Attributes:
        requests_per_second: Maximum requests per second.
        burst_limit: Maximum burst size for token bucket.
        retry_after_header: HTTP header name for retry-after.
    """

    requests_per_second: float = 10.0
    burst_limit: int = 20
    retry_after_header: str = "Retry-After"


@dataclass
class TokenBucket:
    """Token bucket for rate limiting.

    Implements the token bucket algorithm for smooth rate limiting
    with burst support.
    """

    rate: float  # tokens per second
    capacity: int  # maximum tokens
    tokens: float = field(init=False)
    last_update: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        """Initialize token count and timestamp."""
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    async def acquire(self, tokens: int = 1) -> float:
        """Acquire tokens from the bucket.

        Args:
            tokens: Number of tokens to acquire.

        Returns:
            Wait time in seconds (0 if tokens available immediately).
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now

            # Refill tokens based on elapsed time
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.rate,
            )

            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0

            # Calculate wait time for tokens to become available
            needed = tokens - self.tokens
            wait_time = needed / self.rate
            self.tokens = 0
            return wait_time


class RateLimiter:
    """Rate limiter for API calls.

    Uses token bucket algorithm with per-service limits.
    """

    def __init__(self) -> None:
        """Initialize rate limiter with default buckets."""
        self._buckets: dict[str, TokenBucket] = {}
        self._configs: dict[str, RateLimitConfig] = {
            # Jira: 100 requests per minute = ~1.67/s
            "jira": RateLimitConfig(
                requests_per_second=1.5,
                burst_limit=10,
            ),
            # GitHub: 5000 requests per hour = ~1.39/s
            "github": RateLimitConfig(
                requests_per_second=1.0,
                burst_limit=20,
            ),
            # LLM providers vary by tier; keep a conservative default.
            "llm": RateLimitConfig(
                requests_per_second=0.5,
                burst_limit=5,
            ),
            # Backwards-compatible service key for existing callers.
            "anthropic": RateLimitConfig(
                requests_per_second=0.5,
                burst_limit=5,
            ),
        }

    def _get_bucket(self, service: str) -> TokenBucket:
        """Get or create token bucket for service.

        Args:
            service: Service name.

        Returns:
            Token bucket for the service.
        """
        if service not in self._buckets:
            config = self._configs.get(
                service,
                RateLimitConfig(),  # default config
            )
            self._buckets[service] = TokenBucket(
                rate=config.requests_per_second,
                capacity=config.burst_limit,
            )
        return self._buckets[service]

    async def acquire(self, service: str, tokens: int = 1) -> None:
        """Acquire rate limit tokens for a service.

        Blocks until tokens are available.

        Args:
            service: Service name (jira, github, llm).
            tokens: Number of tokens to acquire.
        """
        bucket = self._get_bucket(service)
        wait_time = await bucket.acquire(tokens)

        if wait_time > 0:
            logger.debug(f"Rate limited for {service}: waiting {wait_time:.2f}s")
            await asyncio.sleep(wait_time)

    def configure(self, service: str, config: RateLimitConfig) -> None:
        """Configure rate limits for a service.

        Args:
            service: Service name.
            config: Rate limit configuration.
        """
        self._configs[service] = config
        # Reset bucket to apply new config
        if service in self._buckets:
            del self._buckets[service]

    async def handle_rate_limit_response(
        self,
        service: str,
        retry_after: float | None = None,
    ) -> None:
        """Handle a rate limit response from an API.

        Args:
            service: Service name.
            retry_after: Seconds to wait (from response header).
        """
        if retry_after:
            logger.warning(f"Rate limit hit for {service}: waiting {retry_after:.2f}s")
            await asyncio.sleep(retry_after)
        else:
            # Default backoff
            await asyncio.sleep(5.0)


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance.

    Returns:
        Rate limiter instance.
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


async def rate_limited(
    service: str,
    tokens: int = 1,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for rate-limited async functions.

    Args:
        service: Service name for rate limiting.
        tokens: Tokens to consume per call.

    Returns:
        Decorated function.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            limiter = get_rate_limiter()
            await limiter.acquire(service, tokens)
            return await func(*args, **kwargs)

        return wrapper

    return decorator
