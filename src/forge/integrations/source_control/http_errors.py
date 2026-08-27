"""Shared HTTP-status -> provider-neutral-exception translation.

Applied to every adapter method that calls a provider's REST API, so the
boundary contract documented in errors.py -- "nothing above the adapter
layer touches a provider's own exception types directly" -- holds for
every adapter without each one re-implementing this mapping.
"""

import functools

import httpx

from forge.integrations.source_control.errors import (
    AuthenticationError,
    RateLimitedError,
    TransientProviderError,
)


def translate_provider_errors(provider_name: str):
    """Build a decorator that translates ``httpx`` exceptions into the neutral
    error hierarchy, tagging messages with ``provider_name`` (e.g. "GitHub",
    "GitLab"). Statuses with no generic neutral mapping (404, 409, 422, ...)
    are left for callers to handle themselves and propagate unchanged.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    raise AuthenticationError(
                        f"{provider_name} rejected the request: {exc}"
                    ) from exc
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After")
                    try:
                        parsed_retry_after = float(retry_after) if retry_after else None
                    except ValueError:
                        parsed_retry_after = None
                    raise RateLimitedError(
                        f"{provider_name} rate-limited the request: {exc}",
                        retry_after=parsed_retry_after,
                    ) from exc
                if status >= 500:
                    raise TransientProviderError(
                        f"{provider_name} returned {status}: {exc}"
                    ) from exc
                raise
            except httpx.TransportError as exc:
                raise TransientProviderError(f"{provider_name} request failed: {exc}") from exc

        return wrapper

    return decorator
