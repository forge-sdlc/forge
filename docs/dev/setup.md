# Local Setup

This page is a condensed reference. For the full walkthrough including payload-based testing, Prometheus metrics, Langfuse tracing, and debugging tools, see the [Developer Guide](../developer-guide.md).

## Prerequisites

| Tool | Install |
|------|---------|
| Python 3.11+ | [python.org](https://www.python.org/) |
| uv | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Podman | `brew install podman` / `dnf install podman` |
| Docker Compose | Included with Docker Desktop or `brew install docker-compose` |

External accounts needed: Jira Cloud, GitHub, and LLM backend access through a direct model provider API or Vertex AI.

## Installation

```bash
git clone https://github.com/forge-sdlc/forge.git
cd forge
uv sync
cp .env.example .env
```

## Environment Variables

Edit `.env` — minimum required:

```bash
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USER_EMAIL=you@example.com
JIRA_API_TOKEN=your-jira-api-token

GITHUB_TOKEN=github_pat_your_token

GOOGLE_CLOUD_PROJECT=your-gcp-project
GOOGLE_CLOUD_LOCATION=global
MODEL_CONNECTIONS={"vertex-prod":{"backend":"vertex-ai","project":"your-gcp-project","location":"global","allowed_models":["gemini-3.5-flash"],"capabilities":["tools"]}}
MODEL_DEFAULT={"connection":"vertex-prod","model":"gemini-3.5-flash"}
REDIS_URL=redis://localhost:6380/0
```

For local development, also set:

```bash
FORGE_REQUIRE_PROJECT_CONFIG=false
GITHUB_DEFAULT_REPO=your-org/your-repo
GITHUB_KNOWN_REPOS=your-org/your-repo
```

See [Reference: Configuration](../reference/config.md) for all variables.

## Build the Container Image

```bash
podman build -t forge-dev:latest -f containers/Containerfile containers/
```

## Start Services

```bash
# Redis (the only service using Docker)
docker compose up redis -d

# API server
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0

# Worker (must run on the host — spawns Podman containers)
uv run forge worker
```

## Service Ports

| Service | Port |
|---------|------|
| API server | 8000 |
| Worker metrics | 8001 |
| Redis | 6380 |

## Webhook Setup

For local development, expose your server with [ngrok](https://ngrok.com/):

```bash
ngrok http 8000
```

Then configure Jira and GitHub webhooks to point at the ngrok URL.

See the [Developer Guide](../developer-guide.md) for payload-based testing without live webhooks.
