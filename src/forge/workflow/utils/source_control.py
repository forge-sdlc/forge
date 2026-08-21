"""Node-facing bridge from a repo identifier to a provider adapter.

Wraps the registry so workflow nodes never import a concrete client or
re-derive the resolve() pattern. Local-git plumbing (GitOperations) is
unchanged; only API calls go through the returned adapter.
"""

from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    Provider,
    RepositoryRef,
    ResolvedRepository,
    SourceControlProvider,
)
from forge.integrations.source_control.errors import NotFoundError
from forge.integrations.source_control.registry import get_registry


def resolve_repository(
    identifier: str, provider_hint: Provider | None = None
) -> ResolvedRepository:
    """Resolve a repo identifier (a `repos.yaml` id or a bare `owner/repo`)."""
    if not identifier:
        raise NotFoundError("Cannot resolve an empty repository identifier")
    return get_registry().resolve(identifier, provider_hint=provider_hint)


def get_adapter(identifier: str) -> tuple[RepositoryRef, SourceControlProvider]:
    """Resolve `identifier` to its (RepositoryRef, adapter) pair.

    Raises NotFoundError when the identifier is empty or the provider has no
    registered adapter factory (which would leave `adapter` None).
    """
    resolved = resolve_repository(identifier)
    if resolved.adapter is None:
        raise NotFoundError(
            f"'{identifier}' resolved to provider '{resolved.repo_ref.provider}' "
            "with no registered adapter"
        )
    return resolved.repo_ref, resolved.adapter


def identity_for(repo_ref: RepositoryRef, native_id: str | int | None) -> ChangeRequestIdentity:
    """Build the composite change-request identity for a repo + native PR/MR id."""
    return ChangeRequestIdentity(
        connection=repo_ref.connection,
        repository_id=repo_ref.id,
        native_id=native_id,
    )
