"""Provider-neutral source control exception hierarchy.

Adapters translate provider-specific failures (HTTP status codes, GraphQL
errors) into these at the boundary; nothing above the adapter layer touches
a provider's own exception types directly.
"""


class SourceControlError(Exception):
    """Base exception for all source control provider errors."""


class AuthenticationError(SourceControlError):
    """Raised when a provider rejects the configured credentials."""


class NotFoundError(SourceControlError):
    """Raised when a repository, connection, or provider resource can't be resolved."""


class ConflictError(SourceControlError):
    """Raised for PR-already-exists or fork-diverged-on-sync conditions."""


class RateLimitedError(SourceControlError):
    """Raised when a provider rate-limits a request."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientProviderError(SourceControlError):
    """Raised for retryable 5xx/timeout provider failures."""


class ProviderConfigError(SourceControlError):
    """Raised for invalid connection/repository configuration."""
