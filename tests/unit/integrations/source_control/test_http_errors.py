"""Tests for shared HTTP-error translation decorator.

The translate_provider_errors decorator factory is applied to every adapter
method that calls a provider's REST API, so the boundary contract documented
in errors.py -- "nothing above the adapter layer touches a provider's own
exception types directly" -- holds for every adapter without each one
re-implementing this mapping.
"""

import httpx
import pytest

from forge.integrations.source_control.errors import (
    AuthenticationError,
    RateLimitedError,
    TransientProviderError,
)
from forge.integrations.source_control.http_errors import translate_provider_errors


@translate_provider_errors("TestProvider")
async def _raises(exc):
    raise exc


@pytest.mark.asyncio
async def test_401_maps_to_authentication_error():
    response = httpx.Response(401, request=httpx.Request("GET", "https://example.com"))
    with pytest.raises(AuthenticationError, match="TestProvider"):
        await _raises(httpx.HTTPStatusError("boom", request=response.request, response=response))


@pytest.mark.asyncio
async def test_429_maps_to_rate_limited_with_retry_after():
    response = httpx.Response(
        429, headers={"Retry-After": "12"}, request=httpx.Request("GET", "https://example.com")
    )
    with pytest.raises(RateLimitedError) as exc_info:
        await _raises(httpx.HTTPStatusError("boom", request=response.request, response=response))
    assert exc_info.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_network_failure_maps_to_transient_provider_error():
    with pytest.raises(TransientProviderError):
        await _raises(httpx.ConnectTimeout("timed out"))


@pytest.mark.asyncio
async def test_404_propagates_unchanged():
    response = httpx.Response(404, request=httpx.Request("GET", "https://example.com"))
    with pytest.raises(httpx.HTTPStatusError):
        await _raises(httpx.HTTPStatusError("boom", request=response.request, response=response))
