"""Live project model-policy resolution shared by host and container agents."""

from typing import TYPE_CHECKING

from forge.models.model_policy import ResolvedModelTarget, canonical_policy_key

if TYPE_CHECKING:
    from forge.config import Settings
    from forge.integrations.jira.client import JiraClient


async def resolve_model_target_for_project(
    settings: "Settings",
    project_key: str | None,
    policy_key: str,
    *,
    jira: "JiraClient | None" = None,
) -> ResolvedModelTarget | None:
    """Fetch current Jira policy and resolve one canonical execution target.

    ``None`` preserves the legacy host/container model split when the new
    provider-neutral policy settings have not been configured.
    """
    if not settings.has_explicit_model_policy:
        return None

    canonical_key = canonical_policy_key(policy_key)
    project_policy = {}
    project_default = None
    owned_jira = None
    # Jira projects may only select from explicitly administrator-owned
    # connections. Global-only policy never adds a Jira dependency.
    if project_key and settings.model_connections:
        if jira is None:
            from forge.integrations.jira.client import JiraClient

            owned_jira = JiraClient(settings=settings)
            jira = owned_jira
        try:
            raw_policy = await jira.get_project_property(project_key, "forge.model_policy")
            raw_default = await jira.get_project_property(project_key, "forge.model_default")
            if raw_policy is not None and not isinstance(raw_policy, dict):
                raise ValueError(f"forge.model_policy for {project_key} must be an object")
            if raw_default is not None and not isinstance(raw_default, dict):
                raise ValueError(f"forge.model_default for {project_key} must be an object")
            project_policy = raw_policy or {}
            project_default = raw_default
        finally:
            if owned_jira is not None:
                await owned_jira.close()

    return settings.model_policy_resolver().resolve(canonical_key, project_policy, project_default)
