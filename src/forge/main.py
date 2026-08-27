"""FastAPI application entry point for Forge webhook gateway."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import forge.integrations.source_control.github  # noqa: F401  (registers GitHub adapter factory)
from forge import __version__
from forge.api.middleware.correlation import CorrelationIdMiddleware
from forge.api.routes import github_router, health_router, jira_router, metrics_router
from forge.config import get_settings
from forge.integrations.source_control.registry import get_registry
from forge.observability.config import configure_tracing, shutdown_tracing
from forge.orchestrator.checkpointer import close_redis_pool

# Load .env before configuring logging so LOG_LEVEL is available at startup
load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown."""
    settings = get_settings()
    from forge.utils.logging import log_startup_banner

    log_startup_banner("API Gateway")

    # Load the source-control registry now so a misconfigured repos.yaml
    # (unknown provider, missing credential_env, etc.) fails startup instead
    # of surfacing as a 500 on the first inbound webhook.
    registry = get_registry()

    # Startup - initialize tracing
    if settings.tracing_enabled:
        configure_tracing(
            service_name=settings.otlp_service_name,
            use_console=(settings.log_level == "DEBUG"),
        )
        logger.info("Distributed tracing initialized")

    yield

    # Shutdown
    logger.info("Shutting down Forge...")
    if settings.tracing_enabled:
        await shutdown_tracing()
    await registry.aclose()
    await close_redis_pool()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI instance.
    """
    description = """
## Forge SDLC Orchestrator

AI-powered software development lifecycle orchestration.

### Features

- **Webhook Gateway**: Receives events from Jira and GitHub
- **Workflow Orchestration**: LangGraph-based state machine
- **AI Integration**: Deep Agents-backed planning and code generation
- **Multi-repo Support**: Concurrent execution across repositories

### Workflow

1. Receive Jira ticket creation/update webhook
2. Generate PRD, Spec, Epics, and Tasks using AI
3. Execute code changes in ephemeral workspaces
4. Create PRs and monitor CI/CD
5. AI review before human approval
6. Aggregate status on merge

### Authentication

All webhook endpoints verify signatures:
- **Jira**: HMAC-SHA256 signature in headers
- **GitHub**: HMAC-SHA256 signature in `X-Hub-Signature-256`
"""

    settings = get_settings()

    app = FastAPI(
        title="Forge SDLC Orchestrator",
        description=description,
        version=__version__,
        lifespan=lifespan,
        openapi_tags=[
            {
                "name": "health",
                "description": "Health check and status endpoints",
            },
            {
                "name": "metrics",
                "description": "Prometheus metrics endpoint",
            },
            {
                "name": "jira",
                "description": "Jira webhook endpoints",
            },
            {
                "name": "github",
                "description": "GitHub webhook endpoints",
            },
        ],
        docs_url=None if settings.disable_openapi_docs else "/docs",
        redoc_url=None if settings.disable_openapi_docs else "/redoc",
        openapi_url=None if settings.disable_openapi_docs else "/openapi.json",
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add correlation ID middleware for request tracking
    app.add_middleware(CorrelationIdMiddleware)

    # Register routes
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(jira_router)
    app.include_router(github_router)

    return app


# Create the app instance
app = create_app()


def main() -> None:
    """Run the application with uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "forge.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.log_level == "DEBUG",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
