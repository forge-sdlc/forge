"""Tests for provider-neutral per-stage model policy."""

import pytest

from forge.models.model_policy import ModelPolicyResolver


@pytest.fixture
def resolver() -> ModelPolicyResolver:
    return ModelPolicyResolver(
        connections={
            "vertex": {
                "backend": "vertex-ai",
                "credential_ref": "gcp-workload-identity",
                "project": "prod",
                "allowed_models": ["gemini-pro", "gemini-flash"],
                "capabilities": ["tools"],
            },
            "locked": {
                "backend": "anthropic",
                "credential_ref": "anthropic-session",
                "allowed_models": ["claude-sonnet"],
                "allow_project_override": False,
            },
        },
        policy={"generate_prd": {"connection": "vertex", "model": "gemini-pro"}},
        default={"connection": "vertex", "model": "gemini-flash"},
    )


def test_precedence_and_policy_source(resolver: ModelPolicyResolver) -> None:
    override = {"generate_prd": {"connection": "vertex", "model": "gemini-flash"}}
    assert resolver.resolve("generate_prd", override).policy_source == "project"
    assert resolver.resolve("generate_prd").policy_source == "global"
    assert resolver.resolve("unknown_node").policy_source == "default"


def test_project_wildcard_overrides_all_stages_but_not_explicit_stage(
    resolver: ModelPolicyResolver,
) -> None:
    overrides = {
        "*": {"connection": "vertex", "model": "gemini-flash"},
        "generate_prd": {"connection": "vertex", "model": "gemini-pro"},
    }

    assert resolver.resolve("implement_task", overrides).model == "gemini-flash"
    assert resolver.resolve("generate_prd", overrides).model == "gemini-pro"
    assert "*" not in resolver.resolve_all(overrides)


def test_rejects_unauthorized_connection(resolver: ModelPolicyResolver) -> None:
    override = {"implement_task": {"connection": "locked", "model": "claude-sonnet"}}
    with pytest.raises(ValueError, match="not allowed for project overrides"):
        resolver.resolve("implement_task", override)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"connection": "missing", "model": "gemini-pro"}, "unknown connection"),
        ({"connection": "vertex", "model": "not-allowed"}, "not allowed"),
        (
            {
                "connection": "vertex",
                "model": "gemini-pro",
                "required_capabilities": ["vision"],
            },
            "lacks required capabilities",
        ),
    ],
)
def test_invalid_project_targets_fail_closed(
    resolver: ModelPolicyResolver, target: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        resolver.resolve("implement_task", {"implement_task": target})


def test_trace_metadata_is_non_secret(resolver: ModelPolicyResolver) -> None:
    resolved = resolver.resolve("generate_prd")
    assert resolved.trace_metadata() == {
        "model_backend": "vertex-ai",
        "model_connection": "vertex",
        "model": "gemini-pro",
        "model_policy_key": "generate_prd",
        "model_policy_source": "global",
    }
    assert "credential" not in str(resolved.model_dump())
