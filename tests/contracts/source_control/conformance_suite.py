"""Provider-agnostic conformance tests for SourceControlProvider.

Each function asserts a subset of the protocol contract. Test modules
parameterize these over concrete provider fixtures.
"""
from typing import Any

from forge.integrations.source_control.contracts import (
    EventKind,
    RepositoryRef,
    SourceControlProvider,
)


async def assert_webhook_verification(
    adapter: SourceControlProvider,
    valid_headers: dict[str, str],
    valid_body: bytes,
    invalid_signature_headers: dict[str, str],
) -> None:
    """Assert webhook signature verification works correctly.

    Args:
        adapter: Provider adapter under test
        valid_headers: Headers with correct signature
        valid_body: Request body that matches the signature
        invalid_signature_headers: Headers with wrong signature
    """
    # Valid signature should return True
    assert await adapter.verify_webhook(valid_headers, valid_body)

    # Invalid signature should return False
    assert not await adapter.verify_webhook(invalid_signature_headers, valid_body)


async def assert_webhook_parsing(
    adapter: SourceControlProvider,
    headers: dict[str, str],
    body: bytes,
    resolver: Any,
    expected_kind: EventKind,
    expected_repo_namespace: str,
) -> None:
    """Assert webhook parsing produces correct NormalizedEvent.

    Args:
        adapter: Provider adapter under test
        headers: Webhook request headers
        body: Webhook request body
        resolver: Registry resolver (or mock)
        expected_kind: Expected EventKind value
        expected_repo_namespace: Expected repository namespace
    """
    event = await adapter.parse_webhook(headers, body, resolver)

    assert event.kind == expected_kind
    assert event.repo_ref.namespace == expected_repo_namespace
    assert event.actor.login
    assert event.received_at is not None


async def assert_repository_operations(
    adapter: SourceControlProvider,
    repo_ref: RepositoryRef,
) -> None:
    """Assert repository metadata operations work.

    Args:
        adapter: Provider adapter under test
        repo_ref: Repository to test against
    """
    # Should resolve default branch
    branch = await adapter.resolve_default_branch(repo_ref)
    assert isinstance(branch, str)
    assert len(branch) > 0

    # Should get authenticated identity
    identity = await adapter.get_authenticated_identity(repo_ref)
    assert identity.login
    assert isinstance(identity.is_bot, bool)
