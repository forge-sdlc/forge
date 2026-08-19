"""Tests for the source control exception hierarchy."""

import pytest

from forge.integrations.source_control.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ProviderConfigError,
    RateLimitedError,
    SourceControlError,
    TransientProviderError,
)


@pytest.mark.parametrize(
    "exc_class",
    [
        AuthenticationError,
        ConflictError,
        NotFoundError,
        ProviderConfigError,
        RateLimitedError,
        TransientProviderError,
    ],
)
def test_every_concrete_error_is_a_source_control_error(exc_class):
    assert issubclass(exc_class, SourceControlError)


def test_rate_limited_error_carries_retry_after():
    error = RateLimitedError("rate limited", retry_after=30.0)
    assert error.retry_after == 30.0
    assert str(error) == "rate limited"


def test_rate_limited_error_retry_after_defaults_to_none():
    error = RateLimitedError("rate limited")
    assert error.retry_after is None
