"""Provider-neutral model selection policy.

Policy objects deliberately contain identifiers and tuning options only.  Provider
credentials remain in :mod:`forge.config` and are resolved at the adapter boundary.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Backend = Literal["google-genai", "vertex-ai", "anthropic"]
MAX_MODEL_OUTPUT_TOKENS = 131_072


class ModelConnection(BaseModel):
    """Administrator-owned, non-secret model connection description."""

    model_config = ConfigDict(extra="forbid")

    backend: Backend
    project: str | None = None
    location: str | None = None
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    capabilities: set[str] = Field(default_factory=set)
    allow_project_override: bool = True

    @model_validator(mode="after")
    def validate_connection(self) -> "ModelConnection":
        if self.backend == "vertex-ai" and not self.project:
            raise ValueError("vertex-ai connections require project")
        return self


class ModelTarget(BaseModel):
    """Exact model target selected for a stable node or skill identifier."""

    model_config = ConfigDict(extra="forbid")

    connection: str
    model: str
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0, le=MAX_MODEL_OUTPUT_TOKENS)
    required_capabilities: set[str] = Field(default_factory=set)


class ResolvedModelTarget(ModelTarget):
    """Pinned, trace-safe result of policy evaluation."""

    backend: Backend
    policy_key: str
    policy_source: Literal["project", "project_default", "global", "default"]
    project: str | None = None
    location: str | None = None

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "model_backend": self.backend,
            "model_connection": self.connection,
            "model": self.model,
            "model_policy_key": self.policy_key,
            "model_policy_source": self.policy_source,
        }


KNOWN_MODEL_POLICY_KEYS = (
    "analyze_bug",
    "answer_question",
    "automated_review_triage",
    "bug_local_review",
    "bug_triage",
    "ci_analysis",
    "ci_fix",
    "code_review",
    "decompose_epics",
    "generate_pr_description",
    "generate_prd",
    "generate_spec",
    "generate_tasks",
    "implement_bug_fix",
    "implement_review_analysis",
    "implement_review_fix",
    "implement_task",
    "local_code_review",
    "plan_bug_fix",
    "proposal_review_triage",
    "rebase",
    "reflect_rca",
    "sync_pr_description",
    "task_takeover_execution",
    "task_takeover_planning",
    "task_takeover_question",
    "task_takeover_review",
    "task_takeover_triage",
    "update_docs",
)

# Requirements belong to Forge's execution stages, not Jira project policy.
# Only these text/classification stages explicitly disable agent tools.
_TOOL_FREE_POLICY_KEYS = {
    "automated_review_triage",
    "generate_pr_description",
    "proposal_review_triage",
    "sync_pr_description",
}
REQUIRED_CAPABILITIES_BY_POLICY_KEY: dict[str, frozenset[str]] = {
    key: frozenset({"tools"})
    for key in KNOWN_MODEL_POLICY_KEYS
    if key not in _TOOL_FREE_POLICY_KEYS
}

_POLICY_KEY_ALIASES = {
    "analyze-ci": "ci_analysis",
    "analyze_ci": "ci_analysis",
    "decompose-epics": "decompose_epics",
    "fix-ci": "ci_fix",
    "fix_ci": "ci_fix",
    "generate-pr-body": "generate_pr_description",
    "generate-prd": "generate_prd",
    "generate-spec": "generate_spec",
    "generate-tasks": "generate_tasks",
    "implement_review_analyze": "implement_review_analysis",
    "plan-bug-fix": "plan_bug_fix",
    "sync-pr-description": "sync_pr_description",
    "task-takeover-execution": "task_takeover_execution",
    "task-takeover-planning": "task_takeover_planning",
    "task-takeover-review": "task_takeover_review",
    "task-takeover-triage": "task_takeover_triage",
    "triage-automated-review": "automated_review_triage",
    "triage-bug": "bug_triage",
    "triage-proposal-review-threads": "proposal_review_triage",
}


def canonical_policy_key(runtime_key: str) -> str:
    """Return the advertised stable policy key for a runtime task/node name."""
    normalized = runtime_key.strip()
    canonical = _POLICY_KEY_ALIASES.get(normalized, normalized.replace("-", "_"))
    if canonical not in KNOWN_MODEL_POLICY_KEYS:
        raise ValueError(f"Unknown model policy key '{runtime_key}'")
    return canonical


class ModelPolicyResolver:
    """Validate and resolve project overrides over administrator policy."""

    def __init__(
        self,
        *,
        connections: dict[str, ModelConnection | dict[str, Any]],
        policy: dict[str, ModelTarget | dict[str, Any]],
        default: ModelTarget | dict[str, Any],
    ) -> None:
        self.connections = {
            name: value
            if isinstance(value, ModelConnection)
            else ModelConnection.model_validate(value)
            for name, value in connections.items()
        }
        self.policy = {
            key: value if isinstance(value, ModelTarget) else ModelTarget.model_validate(value)
            for key, value in policy.items()
        }
        self.default = (
            default if isinstance(default, ModelTarget) else ModelTarget.model_validate(default)
        )
        if any(not key for key in self.policy):
            raise ValueError("Model policy keys must not be empty")
        unknown_keys = set(self.policy) - set(KNOWN_MODEL_POLICY_KEYS) - {"*"}
        if unknown_keys:
            raise ValueError(f"Unknown model policy key '{sorted(unknown_keys)[0]}'")
        # Validate administrator policy eagerly at startup.
        for key, target in [*self.policy.items(), ("default", self.default)]:
            self._validate_target(target, project_override=False, key=key)

    def available_models_summary(self) -> str:
        """Return a non-secret summary suitable for operator-facing errors."""
        entries = []
        for name, connection in sorted(self.connections.items()):
            models = ", ".join(connection.allowed_models) or "(none)"
            entries.append(f"{connection.backend}: {name}=[{models}]")
        return "; ".join(entries) or "(no model connections configured)"

    def _configuration_error(self, message: str) -> ValueError:
        return ValueError(
            f"Model policy configuration error: {message}. "
            f"Available connections and models: {self.available_models_summary()}"
        )

    @staticmethod
    def _model_matches_backend(model: str, backend: Backend) -> bool:
        is_gemini = model.lower().startswith(("gemini", "models/gemini"))
        # Vertex AI's model garden intentionally serves both Gemini and
        # Anthropic models. Direct-provider backends accept only their family.
        if backend == "vertex-ai":
            return True
        return is_gemini == (backend == "google-genai")

    def _validate_target(
        self, target: ModelTarget, *, project_override: bool, key: str
    ) -> ModelConnection:
        connection = self.connections.get(target.connection)
        if connection is None:
            raise self._configuration_error(
                f"policy '{key}' references unknown connection '{target.connection}'"
            )
        if project_override and not connection.allow_project_override:
            raise ValueError(
                f"Connection '{target.connection}' is not allowed for project overrides"
            )
        if not self._model_matches_backend(target.model, connection.backend):
            raise self._configuration_error(
                f"model '{target.model}' is incompatible with backend '{connection.backend}'"
            )
        allowed = connection.allowed_models
        if "*" not in allowed and target.model not in allowed:
            raise self._configuration_error(
                f"model '{target.model}' is not allowed on connection '{target.connection}'"
            )
        stage_requirements = REQUIRED_CAPABILITIES_BY_POLICY_KEY.get(key, frozenset())
        missing = (target.required_capabilities | stage_requirements) - connection.capabilities
        if missing:
            raise ValueError(
                f"Connection '{target.connection}' lacks required capabilities: "
                + ", ".join(sorted(missing))
            )
        return connection

    def resolve(
        self,
        key: str,
        project_policy: dict[str, Any] | None = None,
        project_default: ModelTarget | dict[str, Any] | None = None,
    ) -> ResolvedModelTarget:
        if not key:
            raise ValueError("Model policy keys must not be empty")
        if key not in KNOWN_MODEL_POLICY_KEYS:
            raise ValueError(f"Unknown model policy key '{key}'")
        if project_policy and any(not project_key for project_key in project_policy):
            raise ValueError("Model policy keys must not be empty")
        unknown_keys = set(project_policy or {}) - set(KNOWN_MODEL_POLICY_KEYS)
        if unknown_keys:
            raise ValueError(f"Unknown model policy key '{sorted(unknown_keys)[0]}'")
        if project_policy and key in project_policy:
            target = ModelTarget.model_validate(project_policy[key])
            source = "project"
            connection = self._validate_target(target, project_override=True, key=key)
        elif project_default is not None:
            target = (
                project_default
                if isinstance(project_default, ModelTarget)
                else ModelTarget.model_validate(project_default)
            )
            source = "project_default"
            connection = self._validate_target(target, project_override=True, key=key)
        elif key in self.policy:
            target = self.policy[key]
            source = "global"
            connection = self._validate_target(target, project_override=False, key=key)
        elif "*" in self.policy:
            target = self.policy["*"]
            source = "global"
            connection = self._validate_target(target, project_override=False, key=key)
        else:
            target = self.default
            source = "default"
            connection = self._validate_target(target, project_override=False, key=key)
        return ResolvedModelTarget(
            **target.model_dump(),
            backend=connection.backend,
            project=connection.project,
            location=connection.location,
            policy_key=key,
            policy_source=source,
        )

    def resolve_all(
        self,
        project_policy: dict[str, Any] | None = None,
        project_default: ModelTarget | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        keys = set(KNOWN_MODEL_POLICY_KEYS) | set(self.policy) | set(project_policy or {})
        keys.discard("*")
        return {
            key: self.resolve(key, project_policy, project_default).model_dump()
            for key in sorted(keys)
        }
