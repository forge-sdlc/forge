"""Configuration management using Pydantic settings."""

import logging
from functools import cached_property, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from forge.integrations.langfuse.fields import TracingField

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Redis Configuration
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for state persistence and message queue",
    )

    # Jira Configuration
    jira_base_url: str = Field(
        description="Jira instance URL (e.g., https://company.atlassian.net)"
    )
    jira_domain: str = Field(
        default="",
        description="Jira domain for MCP (e.g., company.atlassian.net, derived from base URL if empty)",
    )

    @property
    def jira_domain_resolved(self) -> str:
        """Get Jira domain, derived from base URL if not explicitly set."""
        if self.jira_domain:
            return self.jira_domain
        # Extract domain from base URL (e.g., https://company.atlassian.net -> company.atlassian.net)
        from urllib.parse import urlparse

        parsed = urlparse(self.jira_base_url)
        return parsed.netloc or self.jira_base_url

    jira_api_token: SecretStr = Field(description="Jira API token for authentication")
    jira_user_email: str = Field(description="Email associated with Jira API token")
    jira_webhook_secret: SecretStr = Field(
        default=SecretStr(""), description="Shared secret for Jira webhook validation"
    )
    jira_spec_custom_field: str = Field(
        default="",
        description="Custom field ID for Specification storage (optional)",
    )

    # Jira workflow configuration
    jira_use_labels: bool = Field(
        default=True,
        description="Use labels instead of custom statuses for workflow state",
    )
    jira_store_in_comments: bool = Field(
        default=True,
        description="Store PRD/Spec in comments instead of custom fields",
    )

    @property
    def atlassian_auth_base64(self) -> str:
        """Generate base64-encoded auth string for Atlassian MCP (email:api_token)."""
        import base64

        credentials = f"{self.jira_user_email}:{self.jira_api_token.get_secret_value()}"
        return base64.b64encode(credentials.encode()).decode()

    # GitHub Configuration
    github_token: SecretStr = Field(description="GitHub personal access token")
    github_webhook_secret: SecretStr = Field(
        default=SecretStr(""), description="Shared secret for GitHub webhook validation"
    )
    github_default_repo: str = Field(
        default="",
        description="Default repository (owner/repo format) for tasks without explicit repo assignment",
    )
    github_known_repos: str = Field(
        default="",
        description="Comma-separated list of known repositories (owner/repo format) for repo assignment",
    )
    forge_require_project_config: bool = Field(
        default=True,
        description=(
            "When True (default), forge.repos and forge.default_repo must be set as "
            "Jira project properties; missing config blocks the workflow. "
            "Set to False for local development to fall back to GITHUB_KNOWN_REPOS "
            "and GITHUB_DEFAULT_REPO env vars instead of blocking."
        ),
    )
    github_fork_owner: str = Field(
        default="",
        description="GitHub account/org where forks are created (defaults to authenticated user if empty)",
    )
    git_user_name: str = Field(
        default="Forge",
        description="Git user name for commits made by Forge",
    )
    git_user_email: str = Field(
        default="forge@example.com",
        description="Git user email for commits made by Forge",
    )
    workspace_base_dir: str | None = Field(
        default=None,
        description=(
            "Base directory for worker workspaces. "
            "When set, workspaces are created as subdirectories here instead of "
            "system temp, enabling a shared filesystem across workers. "
            "Unset (default) uses a per-run system temp directory."
        ),
    )

    # PRD Approval Configuration (global fallbacks — per-project config via
    # Jira project property forge.prd_proposals_repo takes precedence)
    prd_proposals_repo: str = Field(
        default="",
        description=(
            "Global fallback GitHub repo (owner/repo) for enhancement proposals. "
            "Per-project config via Jira project property forge.prd_proposals_repo "
            "takes precedence. Only used when forge_require_project_config is False."
        ),
    )
    prd_proposals_path: str = Field(
        default="",
        description=(
            "Base directory in the proposals repo for enhancement folders. "
            "Empty string means repo root. Per-project config via Jira project "
            "property forge.prd_proposals_path takes precedence."
        ),
    )

    @property
    def known_repos(self) -> list[str]:
        """Get list of known repositories."""
        if not self.github_known_repos:
            return []
        return [r.strip() for r in self.github_known_repos.split(",") if r.strip()]

    # Model backend configuration. Provider-specific credentials stay at the
    # adapter boundary; the rest of Forge consumes the resolved backend/model.
    llm_backend: Literal["google-genai", "vertex-ai", "anthropic"] = Field(
        description="Model backend: vertex-ai, google-genai, or anthropic",
    )
    google_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Google Gemini API key for the google-genai backend",
    )
    google_cloud_project: str = Field(
        default="",
        description="Google Cloud project for the vertex-ai backend",
    )
    google_cloud_location: str = Field(
        default="global",
        description="Google Cloud location for the vertex-ai backend",
    )
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key for the anthropic backend",
    )
    llm_model: str = Field(
        description="Model for orchestrator agents",
    )
    container_llm_model: str = Field(
        default="",
        description="Model for container tasks (empty = use llm_model)",
    )
    llm_max_tokens: int = Field(
        default=16384,
        description="Maximum output tokens for LLM responses (default 16384)",
    )
    model_connections: dict[str, Any] = Field(
        default_factory=dict,
        description="Named non-secret model connections (JSON object)",
    )
    model_policy: dict[str, Any] = Field(
        default_factory=dict,
        description="Global stable node/skill to exact model target mapping (JSON object)",
    )
    model_default: dict[str, Any] = Field(
        default_factory=dict,
        description="Global default exact model target (JSON object)",
    )

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_model_fields(cls, values: Any) -> Any:
        """Derive runtime fields from a complete provider-neutral configuration.

        The rest of Forge can continue consuming the established settings
        attributes without forcing operators to configure two sources of truth.
        """
        if not isinstance(values, dict):
            return values
        connections = values.get("model_connections") or {}
        default = values.get("model_default") or {}
        if not connections:
            return values
        if not default:
            if not values.get("llm_backend") or not values.get("llm_model"):
                raise ValueError(
                    "MODEL_DEFAULT is required when MODEL_CONNECTIONS replaces legacy "
                    "LLM_BACKEND/LLM_MODEL configuration"
                )
            return values

        connection_name = default.get("connection")
        connection = connections.get(connection_name)
        if not isinstance(connection, dict):
            raise ValueError(f"MODEL_DEFAULT references unknown connection '{connection_name}'")
        backend = connection.get("backend")
        model = default.get("model")
        if not backend or not model:
            raise ValueError("MODEL_DEFAULT and its connection must define a backend and model")

        derived = dict(values)
        derived["llm_backend"] = backend
        derived["llm_model"] = model
        if backend == "vertex-ai":
            derived["google_cloud_project"] = connection.get("project") or ""
            derived["google_cloud_location"] = connection.get("location") or "global"
        return derived

    @property
    def container_model(self) -> str:
        """Get the container model, falling back to the primary configured model."""
        return self.container_llm_model or self.llm_model

    @staticmethod
    def detect_model_provider(model_name: str) -> str:
        """Detect model provider from model name.

        Returns:
            'anthropic' for Anthropic models, 'google' for Gemini models.
        """
        model_lower = model_name.lower()
        if model_lower.startswith(("gemini", "models/gemini")):
            return "google"
        # Default to anthropic for claude-* or unknown direct-provider models.
        return "anthropic"

    @model_validator(mode="after")
    def validate_container_timeouts(self) -> "Settings":
        """Ensure per-command timeout fits within the container lifetime."""
        if self.container_command_timeout > self.container_timeout:
            raise ValueError(
                f"container_command_timeout ({self.container_command_timeout}s) must not "
                f"exceed container_timeout ({self.container_timeout}s)"
            )
        return self

    @model_validator(mode="after")
    def validate_llm_configuration(self) -> "Settings":
        """Fail at startup when the selected backend cannot serve its models."""
        from forge.models.model_policy import ModelPolicyResolver

        models = [self.llm_model]
        if self.container_llm_model:
            models.append(self.container_llm_model)

        if self.llm_backend == "vertex-ai":
            if not self.google_cloud_project:
                raise ValueError("GOOGLE_CLOUD_PROJECT is required for vertex-ai")
            self._validate_model_policy(ModelPolicyResolver)
            return self

        if self.llm_backend == "google-genai":
            if not self.google_api_key.get_secret_value():
                raise ValueError("GOOGLE_API_KEY is required for google-genai")
            incompatible = [m for m in models if self.detect_model_provider(m) != "google"]
            if incompatible:
                raise ValueError(f"Model '{incompatible[0]}' is not supported by google-genai")
            self._validate_model_policy(ModelPolicyResolver)
            return self

        if not self.anthropic_api_key.get_secret_value():
            raise ValueError("ANTHROPIC_API_KEY is required for anthropic")
        incompatible = [m for m in models if self.detect_model_provider(m) != "anthropic"]
        if incompatible:
            raise ValueError(f"Model '{incompatible[0]}' is not supported by anthropic")
        self._validate_model_policy(ModelPolicyResolver)
        return self

    def _validate_model_policy(self, resolver_type: type) -> None:
        """Validate policy while preserving the legacy single-model configuration."""
        resolver = resolver_type(
            connections=self.effective_model_connections,
            policy=self.model_policy,
            default=self.effective_model_default,
        )
        for connection in resolver.connections.values():
            if connection.backend == "google-genai" and not self.google_api_key.get_secret_value():
                raise ValueError("GOOGLE_API_KEY is required by a google-genai model connection")
            if connection.backend == "anthropic" and not self.anthropic_api_key.get_secret_value():
                raise ValueError("ANTHROPIC_API_KEY is required by an anthropic model connection")

    @property
    def effective_model_connections(self) -> dict[str, Any]:
        if self.model_connections:
            return self.model_connections
        connection: dict[str, Any] = {
            "backend": self.llm_backend,
            "allowed_models": list(dict.fromkeys([self.llm_model, self.container_model])),
            # Legacy Forge agents already rely on provider tool calling. This
            # implicit connection is not exposed to Jira project overrides.
            "capabilities": ["tools"],
        }
        if self.llm_backend == "vertex-ai":
            connection.update(
                project=self.google_cloud_project,
                location=self.google_cloud_location,
            )
        return {"default": connection}

    @property
    def effective_model_default(self) -> dict[str, Any]:
        return self.model_default or {"connection": "default", "model": self.llm_model}

    @property
    def has_explicit_model_policy(self) -> bool:
        """Whether the provider-neutral policy settings were explicitly configured."""
        return bool(self.model_connections or self.model_policy or self.model_default)

    def model_policy_resolver(self):
        from forge.models.model_policy import ModelPolicyResolver

        return ModelPolicyResolver(
            connections=self.effective_model_connections,
            policy=self.model_policy,
            default=self.effective_model_default,
        )

    # Langfuse Configuration
    langfuse_enabled_setting: bool = Field(
        default=True,
        alias="langfuse_enabled",
        description="Enable Langfuse tracing (also requires keys to be set)",
    )
    langfuse_public_key: str = Field(default="", description="Langfuse public key")
    langfuse_secret_key: SecretStr = Field(default=SecretStr(""), description="Langfuse secret key")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", description="Langfuse host URL"
    )
    langfuse_trace_tags: str = Field(
        default="",
        description="Comma-separated list of TracingField names to include as Langfuse trace tags",
    )
    langfuse_trace_metadata: str = Field(
        default="",
        description="Comma-separated list of TracingField names to include as Langfuse trace metadata",
    )

    # Agent Configuration
    agent_enable_tools: bool = Field(
        default=True,
        description="Enable agent tools (Read, Glob, Grep, WebSearch)",
    )
    agent_allowed_tools: str = Field(
        default="*",
        description="Allowed agent tools: '*' for all, or comma-separated list",
    )
    agent_enable_mcp: bool = Field(
        default=True,
        description="Enable MCP server integrations",
    )
    agent_mcp_servers: str = Field(
        default="*",
        description="MCP servers to enable: '*' for all from config, or comma-separated list",
    )
    agent_mcp_read_only: bool = Field(
        default=True,
        description="Restrict MCP tools to read-only operations (no create/update/delete)",
    )
    agent_mcp_config_path: str = Field(
        default="",
        description="Path to MCP servers config file (default: mcp-servers.json in project root)",
    )
    agent_working_directory: str = Field(
        default="",
        description="Working directory for agent file operations (empty = current dir)",
    )
    skills_dir: str = Field(
        default="skills/",
        description="Base directory for skill resolution. The resolver finds skills/default/ and skills/{project}/ under this path.",
    )
    disable_openapi_docs: bool = Field(
        default=False,
        description="Disable /docs, /redoc, and /openapi.json endpoints",
    )

    @property
    def skills_install_dir(self) -> Path:
        """Directory for runtime-fetched skill packages."""
        return Path(self.skills_dir).resolve()

    container_langchain_verbose: bool = Field(
        default=False,
        description="Enable LangChain verbose/debug logging in container",
    )
    container_keep: bool = Field(
        default=False,
        alias="forge_container_keep",
        description=(
            "Keep all containers after exit instead of removing them with --rm. "
            "Useful for debugging: inspect logs with `podman logs <name>` and "
            "filesystem with `podman export <name> | tar -x`. "
            "Set FORGE_CONTAINER_KEEP=true in .env."
        ),
    )
    agent_backend: str = Field(
        default="filesystem",
        description="Deep Agents backend type: filesystem, state, or store",
    )

    # Prompt Configuration
    prompt_version: str = Field(
        default="v1",
        description="Prompt template version to use (e.g., v1, v2)",
    )

    # Application Configuration
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_file: str = Field(
        default="",
        description="Path to log file (empty = stdout only)",
    )
    ci_fix_max_retries: int = Field(
        default=5, description="Maximum retry attempts for autonomous CI fixes"
    )
    ci_ignored_checks: str = Field(
        default="tide",
        description=(
            "Comma-separated substrings of CI check names to ignore when evaluating "
            "pass/fail. Checks whose names contain any of these substrings and have "
            "not completed are silently skipped (e.g. Prow merge-queue checks that "
            "stay pending until merge labels are added). Default: 'tide'."
        ),
    )

    @property
    def ignored_ci_checks(self) -> list[str]:
        """Parsed list of CI check name substrings to ignore."""
        if not self.ci_ignored_checks:
            return []
        return [s.strip() for s in self.ci_ignored_checks.split(",") if s.strip()]

    webhook_ack_timeout: float = Field(
        default=0.5, description="Webhook acknowledgment timeout in seconds"
    )

    # Container Configuration
    container_image: str = Field(
        default="localhost/forge-dev:latest",
        description="Container image for task execution (local or registry URL)",
    )
    container_timeout: int = Field(
        default=1800,
        description="Container execution timeout in seconds (default: 30 minutes)",
    )
    container_command_timeout: int = Field(
        default=600,
        gt=0,
        description="Maximum execution time for individual commands inside containers",
    )
    container_memory: str = Field(
        default="4g",
        description="Container memory limit",
    )
    container_cpus: str = Field(
        default="2",
        description="Container CPU limit",
    )

    # Auto Review Configuration
    auto_review_poll_interval: float = Field(
        default=5.0,
        description="Polling interval in seconds for detecting review cycle files during container execution",
    )
    auto_review_record_polled_files: Literal["log", "copy"] | None = Field(
        default=None,
        description=(
            "Recording mode for polled review cycle files. "
            "'log' logs cycle data at INFO level. "
            "'copy' copies files to {recording_dir}/{step-name}/review_cycle_*.json. "
            "None disables recording."
        ),
    )

    # Queue Consumer Configuration
    queue_max_concurrent_tasks: int = Field(
        default=20,
        description="Maximum number of in-flight message processing tasks in the queue consumer. Prevents resource exhaustion during message bursts.",
    )

    # Worker Metrics Configuration
    worker_metrics_port: int = Field(
        default=8001,
        description="Port for worker Prometheus metrics endpoint",
    )
    worker_metrics_enabled: bool = Field(
        default=True,
        description="Enable Prometheus metrics endpoint in worker",
    )

    # OpenTelemetry Configuration
    otlp_endpoint: str = Field(
        default="",
        description="OTLP endpoint for trace export (e.g., http://localhost:4317)",
    )
    otlp_service_name: str = Field(
        default="forge",
        description="Service name for trace attribution",
    )
    tracing_enabled: bool = Field(
        default=True,
        description="Enable distributed tracing",
    )

    @property
    def langfuse_enabled(self) -> bool:
        """Check if Langfuse tracing is enabled and configured."""
        return bool(
            self.langfuse_enabled_setting
            and self.langfuse_public_key
            and self.langfuse_secret_key.get_secret_value()
        )

    @cached_property
    def trace_tag_fields(self) -> list["TracingField"]:
        """Parse and validate configured Langfuse trace tag fields."""
        from forge.integrations.langfuse.fields import parse_trace_fields

        fields = parse_trace_fields(self.langfuse_trace_tags, allow_tags=True)
        if fields:
            logger.info(
                "Langfuse trace tags configured: %s",
                ", ".join(f.value for f in fields),
            )
        return fields

    @cached_property
    def trace_metadata_fields(self) -> list["TracingField"]:
        """Parse and validate configured Langfuse trace metadata fields."""
        from forge.integrations.langfuse.fields import parse_trace_fields

        fields = parse_trace_fields(self.langfuse_trace_metadata, allow_tags=False)
        if fields:
            logger.info(
                "Langfuse trace metadata configured: %s",
                ", ".join(f.value for f in fields),
            )
        return fields


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
