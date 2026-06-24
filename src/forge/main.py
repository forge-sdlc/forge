"""FastAPI application entry point for Forge webhook gateway."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from forge import __version__
from forge.api.middleware.correlation import CorrelationIdMiddleware
from forge.api.routes import (
    github_router,
    health_router,
    jira_router,
    metrics_router,
    observability_router,
    sessions_router,
)
from forge.config import get_settings
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
    logger.info(f"Starting Forge v{__version__} ({settings.log_level})")

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
- **AI Integration**: Claude-powered planning and code generation
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
            {
                "name": "sessions",
                "description": "Read-only Forge session inspection endpoints",
            },
            {
                "name": "observability",
                "description": "Read-only observability data access endpoints",
            },
        ],
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
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
    app.include_router(sessions_router)
    app.include_router(observability_router)

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
