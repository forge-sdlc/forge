"""Provider-neutral model selection policy.

Policy objects deliberately contain identifiers and tuning options only.  Provider
credentials remain in :mod:`forge.config` and are resolved at the adapter boundary.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Backend = Literal["google-genai", "vertex-ai", "anthropic"]


class ModelConnection(BaseModel):
    """Administrator-owned, non-secret model connection description."""

    model_config = ConfigDict(extra="forbid")

    backend: Backend
    credential_ref: str = "default"
    project: str | None = None
    location: str | None = None
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    capabilities: set[str] = Field(default_factory=set)
    allow_project_override: bool = True

    @model_validator(mode="after")
    def validate_connection(self) -> "ModelConnection":
        if not self.credential_ref or any(
            marker in self.credential_ref.lower() for marker in ("api_key=", "token=", "secret=")
        ):
            raise ValueError("credential_ref must be a non-secret credential identifier")
        if self.backend == "vertex-ai" and not self.project:
            raise ValueError("vertex-ai connections require project")
        return self


class ModelTarget(BaseModel):
    """Exact model target selected for a stable node or skill identifier."""

    model_config = ConfigDict(extra="forbid")

    connection: str
    model: str
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, gt=0)
    required_capabilities: set[str] = Field(default_factory=set)


class ResolvedModelTarget(ModelTarget):
    """Pinned, trace-safe result of policy evaluation."""

    backend: Backend
    policy_key: str
    policy_source: Literal["project", "global", "default"]
    project: str | None = None
    location: str | None = None
    credential_ref: str = Field(default="", exclude=True, repr=False)

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "model_backend": self.backend,
            "model_connection": self.connection,
            "model": self.model,
            "model_policy_key": self.policy_key,
            "model_policy_source": self.policy_source,
        }


KNOWN_MODEL_POLICY_KEYS = (
    "generate_prd",
    "generate_spec",
    "decompose_epics",
    "generate_tasks",
    "triage",
    "plan_bug_fix",
    "implement_task",
    "implement_bug_fix",
    "local_review",
    "code_review",
    "generate_pr_description",
    "ci_fix",
    "answer_question",
)


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
        # Validate administrator policy eagerly at startup.
        for key, target in [*self.policy.items(), ("default", self.default)]:
            self._validate_target(target, project_override=False, key=key)

    @staticmethod
    def _model_matches_backend(model: str, backend: Backend) -> bool:
        is_gemini = model.lower().startswith(("gemini", "models/gemini"))
        return backend == "vertex-ai" or (is_gemini == (backend == "google-genai"))

    def _validate_target(
        self, target: ModelTarget, *, project_override: bool, key: str
    ) -> ModelConnection:
        connection = self.connections.get(target.connection)
        if connection is None:
            raise ValueError(
                f"Model policy '{key}' references unknown connection '{target.connection}'"
            )
        if project_override and not connection.allow_project_override:
            raise ValueError(
                f"Connection '{target.connection}' is not allowed for project overrides"
            )
        if not self._model_matches_backend(target.model, connection.backend):
            raise ValueError(
                f"Model '{target.model}' is incompatible with backend '{connection.backend}'"
            )
        allowed = connection.allowed_models
        if "*" not in allowed and target.model not in allowed:
            raise ValueError(
                f"Model '{target.model}' is not allowed on connection '{target.connection}'"
            )
        missing = target.required_capabilities - connection.capabilities
        if missing:
            raise ValueError(
                f"Connection '{target.connection}' lacks required capabilities: "
                + ", ".join(sorted(missing))
            )
        return connection

    def resolve(
        self, key: str, project_policy: dict[str, Any] | None = None
    ) -> ResolvedModelTarget:
        if project_policy and key in project_policy:
            target = ModelTarget.model_validate(project_policy[key])
            source = "project"
            connection = self._validate_target(target, project_override=True, key=key)
        elif key in self.policy:
            target = self.policy[key]
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
            credential_ref=connection.credential_ref,
            policy_key=key,
            policy_source=source,
        )

    def resolve_all(self, project_policy: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = set(KNOWN_MODEL_POLICY_KEYS) | set(self.policy) | set(project_policy or {})
        return {key: self.resolve(key, project_policy).model_dump() for key in sorted(keys)}
