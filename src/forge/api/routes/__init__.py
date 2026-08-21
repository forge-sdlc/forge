"""API route modules."""

from forge.api.routes.github import router as github_router
from forge.api.routes.gitlab import router as gitlab_router
from forge.api.routes.health import router as health_router
from forge.api.routes.jira import router as jira_router
from forge.api.routes.metrics import router as metrics_router

__all__ = [
    "github_router",
    "gitlab_router",
    "health_router",
    "jira_router",
    "metrics_router",
]
