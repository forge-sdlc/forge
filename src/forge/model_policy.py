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

    ``None`` preserves the legacy host/container model split when neither a
    project policy nor provider-neutral environment policy is configured.
    """
    canonical_key = canonical_policy_key(policy_key)
    project_policy = {}
    owned_jira = None
    if project_key:
        if jira is None:
            from forge.integrations.jira.client import JiraClient

            owned_jira = JiraClient(settings=settings)
            jira = owned_jira
        try:
            raw = await jira.get_project_property(project_key, "forge.model_policy")
            if raw is not None and not isinstance(raw, dict):
                raise ValueError(f"forge.model_policy for {project_key} must be an object")
            project_policy = raw or {}
        finally:
            if owned_jira is not None:
                await owned_jira.close()

    if not project_policy and not settings.has_explicit_model_policy:
        return None

    return settings.model_policy_resolver().resolve(canonical_key, project_policy)
