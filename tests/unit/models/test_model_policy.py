"""Tests for provider-neutral per-stage model policy."""

import pytest

from forge.models.model_policy import (
    KNOWN_MODEL_POLICY_KEYS,
    REQUIRED_CAPABILITIES_BY_POLICY_KEY,
    ModelPolicyResolver,
    canonical_policy_key,
)


@pytest.fixture
def resolver() -> ModelPolicyResolver:
    return ModelPolicyResolver(
        connections={
            "vertex": {
                "backend": "vertex-ai",
                "project": "prod",
                "allowed_models": ["gemini-pro", "gemini-flash"],
                "capabilities": ["tools"],
            },
            "locked": {
                "backend": "anthropic",
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
    assert resolver.resolve("bug_triage").policy_source == "default"


def test_project_default_overrides_global_policy_but_not_explicit_project_stage(
    resolver: ModelPolicyResolver,
) -> None:
    overrides = {
        "generate_prd": {"connection": "vertex", "model": "gemini-pro"},
    }
    project_default = {"connection": "vertex", "model": "gemini-flash"}

    resolved_default = resolver.resolve("implement_task", overrides, project_default)
    resolved_explicit = resolver.resolve("generate_prd", overrides, project_default)
    assert resolved_default.model == "gemini-flash"
    assert resolved_default.policy_source == "project_default"
    assert resolved_explicit.model == "gemini-pro"
    assert resolved_explicit.policy_source == "project"


def test_project_policy_rejects_legacy_wildcard(resolver: ModelPolicyResolver) -> None:
    with pytest.raises(ValueError, match="Unknown model policy key '\\*'"):
        resolver.resolve(
            "generate_prd",
            {"*": {"connection": "vertex", "model": "gemini-flash"}},
        )


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


def test_stage_capabilities_cannot_be_weakened_by_project_policy() -> None:
    resolver = ModelPolicyResolver(
        connections={"vertex": {"backend": "vertex-ai", "project": "prod"}},
        policy={},
        default={"connection": "vertex", "model": "gemini-pro"},
    )

    with pytest.raises(ValueError, match="lacks required capabilities: tools"):
        resolver.resolve(
            "implement_task",
            {"implement_task": {"connection": "vertex", "model": "gemini-pro"}},
        )


def test_tool_requirements_cover_every_agentic_stage() -> None:
    tool_free = {
        "automated_review_triage",
        "generate_pr_description",
        "proposal_review_triage",
        "sync_pr_description",
    }
    assert set(REQUIRED_CAPABILITIES_BY_POLICY_KEY) == set(KNOWN_MODEL_POLICY_KEYS) - tool_free


def test_project_output_token_limit_is_bounded(resolver: ModelPolicyResolver) -> None:
    with pytest.raises(ValueError, match="less than or equal to 131072"):
        resolver.resolve(
            "generate_prd",
            {
                "generate_prd": {
                    "connection": "vertex",
                    "model": "gemini-pro",
                    "max_output_tokens": 131_073,
                }
            },
        )


def test_trace_metadata_is_non_secret(resolver: ModelPolicyResolver) -> None:
    resolved = resolver.resolve("generate_prd")
    assert resolved.trace_metadata() == {
        "model_backend": "vertex-ai",
        "model_connection": "vertex",
        "model": "gemini-pro",
        "model_policy_key": "generate_prd",
        "model_policy_source": "global",
    }
    assert "credential_ref" not in resolved.model_dump()


def test_provider_neutral_options_flow_to_resolved_target(
    resolver: ModelPolicyResolver,
) -> None:
    override = {
        "generate_prd": {
            "connection": "vertex",
            "model": "gemini-pro",
            "temperature": 0.25,
            "max_output_tokens": 8192,
        }
    }

    resolved = resolver.resolve("generate_prd", override)

    assert resolved.temperature == 0.25
    assert resolved.max_output_tokens == 8192


def test_openai_compatible_connection_flows_endpoint_without_secret() -> None:
    resolver = ModelPolicyResolver(
        connections={
            "gateway": {
                "backend": "openai-compatible",
                "base_url": "https://gateway.example/v1",
                "api_key_env": "GATEWAY_API_KEY",
                "capabilities": ["tools"],
            }
        },
        policy={},
        default={"connection": "gateway", "model": "custom-model"},
    )

    resolved = resolver.resolve("implement_task")

    assert resolved.base_url == "https://gateway.example/v1"
    assert resolved.api_key_env == "GATEWAY_API_KEY"
    assert "api_key" not in resolved.model_dump()


@pytest.mark.parametrize("base_url", ["", "gateway.example/v1", "ftp://gateway.example/v1"])
def test_openai_compatible_connection_requires_http_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP.*base_url"):
        ModelPolicyResolver(
            connections={"gateway": {"backend": "openai-compatible", "base_url": base_url}},
            policy={},
            default={"connection": "gateway", "model": "custom-model"},
        )


def test_openai_compatible_connection_validates_api_key_env() -> None:
    with pytest.raises(ValueError, match="valid environment variable"):
        ModelPolicyResolver(
            connections={
                "gateway": {
                    "backend": "openai-compatible",
                    "base_url": "https://gateway.example/v1",
                    "api_key_env": "not-valid!",
                }
            },
            policy={},
            default={"connection": "gateway", "model": "custom-model"},
        )


def test_invalid_default_connection_fails_eagerly() -> None:
    with pytest.raises(ValueError, match="unknown connection"):
        ModelPolicyResolver(
            connections={},
            policy={},
            default={"connection": "missing", "model": "gemini-pro"},
        )


def test_unknown_connection_error_lists_available_backends_and_models(
    resolver: ModelPolicyResolver,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolver.resolve(
            "generate_prd",
            {"generate_prd": {"connection": "missing", "model": "gemini-pro"}},
        )

    message = str(exc_info.value)
    assert "unknown connection 'missing'" in message
    assert "vertex-ai: vertex=[gemini-pro, gemini-flash]" in message
    assert "anthropic: locked=[claude-sonnet]" in message


def test_disallowed_model_error_lists_available_backends_and_models(
    resolver: ModelPolicyResolver,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolver.resolve(
            "generate_prd",
            {"generate_prd": {"connection": "vertex", "model": "gemini-ultra"}},
        )

    message = str(exc_info.value)
    assert "model 'gemini-ultra' is not allowed on connection 'vertex'" in message
    assert "vertex-ai: vertex=[gemini-pro, gemini-flash]" in message


def test_empty_policy_key_is_rejected(resolver: ModelPolicyResolver) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        resolver.resolve("generate_prd", {"": {"connection": "vertex", "model": "gemini-pro"}})


@pytest.mark.parametrize(
    ("runtime_key", "expected"),
    [
        ("triage-bug", "bug_triage"),
        ("generate-pr-body", "generate_pr_description"),
        ("sync-pr-description", "sync_pr_description"),
        ("fix_ci", "ci_fix"),
        ("implement_review_fix", "implement_review_fix"),
    ],
)
def test_runtime_names_have_canonical_policy_keys(runtime_key: str, expected: str) -> None:
    assert canonical_policy_key(runtime_key) == expected


def test_every_advertised_policy_key_resolves(resolver: ModelPolicyResolver) -> None:
    resolved = resolver.resolve_all()
    assert set(resolved) == set(KNOWN_MODEL_POLICY_KEYS)
    assert isinstance(resolved["implement_task"]["required_capabilities"], list)


def test_advertised_policy_keys_are_sorted() -> None:
    assert tuple(sorted(KNOWN_MODEL_POLICY_KEYS)) == KNOWN_MODEL_POLICY_KEYS


def test_unknown_policy_key_is_rejected(resolver: ModelPolicyResolver) -> None:
    with pytest.raises(ValueError, match="Unknown model policy key"):
        resolver.resolve(
            "generate_prd", {"typo_stage": {"connection": "vertex", "model": "gemini-pro"}}
        )


def test_unknown_resolve_key_is_rejected(resolver: ModelPolicyResolver) -> None:
    with pytest.raises(ValueError, match="Unknown model policy key 'unknown_node'"):
        resolver.resolve("unknown_node")
