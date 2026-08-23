import pytest

from forge.integrations.source_control.contracts import (
    ChangeRequestIdentity,
    RepositoryRef,
    Provider,
)
from forge.integrations.source_control.errors import NotFoundError
from forge.workflow.utils.source_control import get_adapter, identity_for


def test_get_adapter_returns_repo_ref_and_adapter():
    import forge.config as config_module
    import forge.integrations.source_control.github  # noqa: F401  (register factory)
    import forge.integrations.source_control.registry as registry_module

    registry_module.get_registry.cache_clear()
    config_module.get_settings.cache_clear()
    try:
        repo_ref, adapter = get_adapter("acme/widgets")
        assert repo_ref.namespace == "acme/widgets"
        assert repo_ref.provider == Provider.GITHUB
        assert adapter is not None
    finally:
        registry_module.get_registry.cache_clear()
        config_module.get_settings.cache_clear()


def test_get_adapter_rejects_empty_identifier():
    with pytest.raises(NotFoundError):
        get_adapter("")


def test_identity_for_builds_composite_identity():
    repo_ref = RepositoryRef(
        id="acme/widgets",
        provider=Provider.GITHUB,
        connection="github-default",
        namespace="acme/widgets",
        default_branch="main",
        change_request_mode="fork",
    )
    identity = identity_for(repo_ref, 42)
    assert identity == ChangeRequestIdentity(
        connection="github-default", repository_id="acme/widgets", native_id=42
    )
