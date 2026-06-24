"""API route modules."""

from forge.api.routes.github import router as github_router
from forge.api.routes.health import router as health_router
from forge.api.routes.jira import router as jira_router
from forge.api.routes.metrics import router as metrics_router
from forge.api.routes.observability import router as observability_router
from forge.api.routes.sessions import router as sessions_router

__all__ = [
    "github_router",
    "health_router",
    "jira_router",
    "metrics_router",
    "observability_router",
    "sessions_router",
]
