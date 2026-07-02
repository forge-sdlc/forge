"""Configuration management using Pydantic settings."""

import logging
from functools import cached_property, lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr
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

    # Anthropic Configuration
    # Option 1: Direct Anthropic API
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key for Claude (leave empty for Vertex AI)",
    )
    # Option 2: Google Vertex AI (supports Claude and Gemini)
    anthropic_vertex_project_id: str = Field(
        default="",
        description="Google Cloud project ID for Vertex AI",
    )
    anthropic_vertex_region: str = Field(
        default="us-east5",
        description="Google Cloud region for Vertex AI (e.g., us-east5)",
    )
    # Model configuration (supports Claude and Gemini on Vertex AI)
    # Claude models: claude-opus-4-5@20251101, claude-sonnet-4-5@20250929, etc.
    # Gemini models: gemini-2.5-pro, gemini-2.5-flash, gemini-3.1-pro-preview, etc.
    llm_model: str = Field(
        default="claude-sonnet-4-5@20250929",
        description="Model for orchestrator (Claude or Gemini on Vertex AI)",
    )
    container_llm_model: str = Field(
        default="",
        description="Model for container tasks (empty = use llm_model)",
    )
    llm_max_tokens: int = Field(
        default=16384,
        description="Maximum output tokens for LLM responses (default 16384)",
    )

    @property
    def container_model(self) -> str:
        """Get model for container execution, falling back to default model."""
        return self.container_llm_model or self.llm_model

    # Backwards compatibility aliases
    @property
    def claude_model(self) -> str:
        """Alias for llm_model (backwards compatibility)."""
        return self.llm_model

    @staticmethod
    def detect_model_provider(model_name: str) -> str:
        """Detect model provider from model name.

        Returns:
            'anthropic' for Claude models, 'google' for Gemini models.
        """
        model_lower = model_name.lower()
        if model_lower.startswith(("gemini", "models/gemini")):
            return "google"
        # Default to anthropic for claude-* or unknown models
        return "anthropic"

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

    # Claude Agent SDK Configuration
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
    container_memory: str = Field(
        default="4g",
        description="Container memory limit",
    )
    container_cpus: str = Field(
        default="2",
        description="Container CPU limit",
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

    @property
    def use_vertex_ai(self) -> bool:
        """Check if using Vertex AI instead of direct Anthropic API."""
        return bool(
            self.anthropic_vertex_project_id and not self.anthropic_api_key.get_secret_value()
        )


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()
