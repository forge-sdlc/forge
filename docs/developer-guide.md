# Forge Developer Guide

Everything you need to run Forge locally, test it, observe what it's doing, and debug problems.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Initial Setup](#2-initial-setup)
3. [Environment Configuration](#3-environment-configuration)
   - [Per-Project Repo Config (Production)](#per-project-repository-configuration-required-in-production)
   - [Local Dev Fallback Mode](#local-development-env-var-fallback-mode)
4. [Running Services](#4-running-services)
5. [Running Tests](#5-running-tests)
6. [Testing with Payloads](#6-testing-with-payloads)
7. [GitHub Webhook Testing](#7-github-webhook-testing)
8. [Prometheus Metrics](#8-prometheus-metrics)
9. [Langfuse Tracing](#9-langfuse-tracing)
10. [Debugging Tools](#10-debugging-tools)
11. [Common Workflows](#11-common-workflows)
12. [Service Reference](#12-service-reference)

---

## 1. Prerequisites

- **Python 3.11+** with [uv](https://github.com/astral-sh/uv)
- **Podman** — for running task containers (`dnf install podman` / `brew install podman`)
- **Docker Compose** — for Redis and API gateway (`dnf install docker-compose` / included with Docker Desktop)
- **Jira Cloud** account with API access
- **GitHub** account with a Personal Access Token (scopes: `repo`, `read:org`)
- **Claude API key** (Anthropic direct) OR Google Cloud project with Vertex AI enabled

---

## 2. Initial Setup

```bash
# Clone and install Python dependencies
git clone https://github.com/your-org/forge.git
cd forge
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env — at minimum fill in Jira, GitHub, and LLM credentials

# Build the task container image
podman build -t forge-dev:latest -f containers/Containerfile containers/
```

---

## 3. Environment Configuration

All settings live in `.env`. The most important ones for local development:

### Required

```bash
# Jira
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USER_EMAIL=you@example.com
JIRA_API_TOKEN=your-jira-api-token

# GitHub
GITHUB_TOKEN=github_pat_your_token

# LLM — choose one backend

# Option A: Anthropic direct
ANTHROPIC_API_KEY=sk-ant-your-key

# Option B: Google Vertex AI (supports Claude + Gemini)
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
ANTHROPIC_VERTEX_REGION=us-east5

# Which model to use
LLM_MODEL=claude-opus-4-5@20251101
```

---

### ⚠️ Per-Project Repository Configuration (Required in Production)

> **Forge does not use global `GITHUB_KNOWN_REPOS` / `GITHUB_DEFAULT_REPO` in production.**
> Repository configuration is set **per Jira project** as project properties.
> If not configured, Forge will block the workflow and post detailed instructions on the ticket.

Each Jira project that Forge manages needs two properties set by a project admin:

| Property key | Type | Description |
|---|---|---|
| `forge.repos` | JSON array of strings | Repos available to this project |
| `forge.default_repo` | JSON string | Repo to use when no explicit assignment is made |

#### Setting properties via the Jira REST API

```bash
# Set the available repos for a project
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.repos" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '["acme/backend", "acme/frontend"]'

# Set the default repo
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.default_repo" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '"acme/backend"'
```

#### What happens when a property is missing

Forge posts this comment on the ticket and sets `forge:blocked`:

```
⚠️ Forge configuration required for project MYPROJ

This ticket cannot be processed because no repository configuration
has been set for this Jira project.

To fix this, a Jira project admin must set the following project property:

  Key:   forge.repos
  Value: ["owner/repo-name", "owner/other-repo"]

Optionally, also set:

  Key:   forge.default_repo
  Value: "owner/repo-name"

Once set, add the label `forge:retry` to this ticket to resume.
```

---

### 🛠️ Local Development: Env Var Fallback Mode

For local development you may not want to configure Jira project properties for every test project. Set this flag to fall back to env vars instead:

```bash
# .env — local dev only, do NOT use in production
FORGE_REQUIRE_PROJECT_CONFIG=false
GITHUB_KNOWN_REPOS=acme/backend,acme/frontend   # fallback repos when forge.repos not set
GITHUB_DEFAULT_REPO=acme/backend                  # fallback when forge.default_repo not set
```

When `FORGE_REQUIRE_PROJECT_CONFIG=false`:
- Missing `forge.repos` → warns in the log and falls back to `GITHUB_KNOWN_REPOS`
- Missing `forge.default_repo` → falls back to `GITHUB_DEFAULT_REPO` silently
- No blocking comment is posted to Jira

> **Default is `true`.** The fallback flag exists only for local dev convenience. Production deployments should always leave it unset (or explicitly `true`) and configure project properties properly.

---

### For Local Testing (skip webhook validation)

```bash
JIRA_WEBHOOK_SECRET=          # empty = no signature check
GITHUB_WEBHOOK_SECRET=        # empty = no signature check
```

### Redis

```bash
REDIS_URL=redis://localhost:6380/0   # matches docker-compose port mapping
```

### Container execution

```bash
CONTAINER_IMAGE=localhost/forge-dev:latest   # built with podman above
CONTAINER_TIMEOUT=1800                        # 30 minutes max
CONTAINER_MEMORY=4g
CONTAINER_CPUS=2
```

### Observability

```bash
# Langfuse (LLM call tracing)
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_TRACE_TAGS=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,llm_model
LANGFUSE_TRACE_METADATA=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,retry_count,system_prompt_length,llm_model

# Grafana dashboard stack
GRAFANA_PORT=3010
LANGFUSE_DOCKER_NETWORK=langfuse_default
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_PORT=9000
CLICKHOUSE_DATABASE=default
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=clickhouse

# CI behaviour
CI_FIX_MAX_RETRIES=5
CI_IGNORED_CHECKS=tide          # comma-separated substrings of checks to ignore
```

---

## 4. Running Services

### Start Redis

Redis is the only service that runs in Docker for local development.

```bash
docker compose up redis -d
```

This starts **Redis Stack** on `localhost:6380` — state, queue, and checkpoints.

### Start the API server

```bash
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0
```

The `--reload` flag restarts the server automatically when source files change — useful during development.

### Start the worker

The worker spawns Podman containers for task execution; it must run on the host.

```bash
uv run forge worker
```

> **Why not in Docker?** The worker spawns Podman containers. Running it inside Docker would require socket mounting which is not supported in this setup.

### Start Prometheus and Grafana (optional, for observability)

```bash
docker compose --env-file .env -f docker-compose.yml up -d prometheus grafana
# Prometheus at http://localhost:9092
# Grafana dashboards at http://localhost:3010
```

**With self-hosted Langfuse** — add the network override so Grafana can reach ClickHouse:

```bash
docker compose --env-file .env \
  -f docker-compose.yml \
  -f devtools/grafana/compose.langfuse-network.yml \
  up -d prometheus grafana
```

> `compose.langfuse-network.yml` requires the `langfuse_default` Docker network to already exist. Omit it if you are not running self-hosted Langfuse — the Prometheus and Redis datasources work without it.

### Full local stack

```bash
# Terminal 1 — Redis, Prometheus, and Grafana
docker compose --env-file .env -f docker-compose.yml up -d redis prometheus grafana

# Terminal 2 — API server
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0

# Terminal 3 — Worker
uv run forge worker
```

### Health check

```bash
curl http://localhost:8000/api/v1/health
```

---

## 5. Running Tests

```bash
# Full test suite
uv run pytest

# Unit tests only (fast)
uv run pytest tests/unit/ -v

# Flow/scenario tests
uv run pytest tests/flows/ -v

# Single file
uv run pytest tests/unit/workflow/test_ci_gate_skip.py -v

# With output (useful for debugging)
uv run pytest tests/unit/ -s

# Linting
uv run ruff check src/

# Type checking
uv run mypy src/forge/
```

The pre-existing failures in `tests/flows/status_transitions/` and a few other files are known issues unrelated to current work — ignore them.

---

## 6. Testing with Payloads

Sample Jira webhook payloads live in `tests/payloads/`. They simulate the full feature workflow from creation to task approval.

### Endpoint

```
POST http://localhost:8000/api/v1/webhooks/jira
Content-Type: application/json
```

### Complete feature workflow sequence

```bash
# 1. Create a feature ticket → Forge generates PRD
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/01-feature-created.json

# 2. Request PRD revision (comment with feedback)
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/02-prd-revision-requested.json

# 3. Approve PRD → Forge generates spec
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/03-prd-approved.json

# 4–9. Continue: spec, plan, tasks (same pattern)
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/05-spec-approved.json

curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/07-plan-approved.json

curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/09-task-approved.json
# → Implementation starts
```

### Q&A mode (ask without triggering regeneration)

Comments starting with `?` or `@forge ask` ask a question instead of requesting a revision:

```bash
# Ask about the PRD
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/10-prd-question.json
```

Supported prefixes (case-insensitive):
- `?Why did you choose REST?`
- `@forge ask explain the auth approach`

### Editing ticket keys in payloads

The payloads use `TEST-123` as a placeholder. Replace it with your actual ticket:

```bash
sed 's/TEST-123/AISOS-999/g' tests/payloads/01-feature-created.json | \
  curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @-
```

### Bug workflow

```bash
# Create a bug → Forge generates RCA
# (use a payload with issuetype: Bug)
```

---

## 7. GitHub Webhook Testing

GitHub webhooks require the `X-GitHub-Event` header. Always include it.

### CI check result (triggers CI evaluation)

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: check_run" \
  -d '{
    "action": "completed",
    "check_run": {
      "status": "completed",
      "conclusion": "failure",
      "head_sha": "YOUR_HEAD_SHA",
      "pull_requests": [
        {"number": 42, "head": {"ref": "forge/ticket-key"}}
      ]
    },
    "repository": {"full_name": "org/repo"},
    "sender": {"login": "your-username"}
  }'
```

Get the current head SHA for a PR:
```bash
gh pr view 42 --repo org/repo --json headRefOid,headRefName
```

### CI gate skip (skip a failing check by name)

Post this as a PR comment:

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issue_comment" \
  -d '{
    "action": "created",
    "issue": {
      "number": 42,
      "title": "[TICKET-123] Your PR title",
      "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/42"}
    },
    "comment": {"body": "/forge skip-gate e2e-openstack"},
    "repository": {"full_name": "org/repo"},
    "sender": {"login": "your-username"}
  }'
```

To remove a skip: change `skip-gate` to `unskip-gate`.

The check name is matched as a **case-insensitive substring** — `e2e-openstack` skips any check whose name contains that string.

### PR review (triggers human review gate)

```bash
# Approved review → advances to complete_tasks
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request_review" \
  -d '{
    "action": "submitted",
    "review": {
      "state": "approved",
      "body": "LGTM",
      "commit_id": "YOUR_HEAD_SHA"
    },
    "pull_request": {
      "number": 42,
      "title": "[TICKET-123] Your PR title",
      "head": {"ref": "forge/ticket-123", "sha": "YOUR_HEAD_SHA"},
      "state": "open"
    },
    "repository": {"full_name": "org/repo"},
    "sender": {"login": "your-username"}
  }'

# Changes requested / comment → triggers implement_review
# Same payload but: "state": "changes_requested" or "state": "commented"
```

> **Note:** Comments (`state: "commented"`) are treated as changes_requested — if your review has comments, Forge will address them.

---

## 8. Prometheus Metrics

### Endpoints

| Source | URL |
|--------|-----|
| API server | `http://localhost:8000/metrics` |
| Worker | `http://localhost:8001/metrics` |
| Prometheus UI | `http://localhost:9092` |
| Grafana dashboards | `http://localhost:3010` |

### Key metrics to watch

```
# Workflow throughput
forge_workflows_started_total
forge_workflows_completed_total
forge_workflows_failed_total

# CI fixing
forge_ci_fix_attempts_total

# Agent performance
forge_agent_duration_seconds     # histogram of agent execution time
forge_phase_duration_seconds     # time per workflow phase

# Webhook health
forge_webhooks_received_total
forge_webhooks_processed_total
forge_webhooks_failed_total

# External API latency
forge_external_api_latency_seconds{service="jira"}
forge_external_api_latency_seconds{service="github"}
forge_external_api_latency_seconds{service="claude"}
```

### Adding worker metrics to Prometheus

The default `prometheus.yml` only scrapes the API server. Add this to scrape the worker:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'forge-worker'
    static_configs:
      - targets: ['host.docker.internal:8001']   # on macOS/Windows
      # or on Linux:
      # - targets: ['172.17.0.1:8001']           # docker bridge gateway
    metrics_path: /metrics
```

Reload Prometheus after editing:
```bash
curl -X POST http://localhost:9092/-/reload
```

### Quick spot check

```bash
# Check workflow counts
curl -s http://localhost:8000/metrics | grep forge_workflows

# Check if worker is serving metrics
curl -s http://localhost:8001/metrics | grep forge_agent
```

---

## 9. Langfuse Tracing

Langfuse records every LLM call: prompt, response, latency, cost, token count.

### Setup

1. Create an account at [cloud.langfuse.com](https://cloud.langfuse.com)
2. Create a project, get your keys
3. Add to `.env`:

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

4. Restart the worker — traces appear in the Langfuse dashboard immediately

### Self-hosted Langfuse (optional)

Run Langfuse locally with Docker:

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d
```

Then set `LANGFUSE_HOST=http://localhost:3000` in `.env`.

### What gets traced

- PRD / spec / epic / task generation calls
- Q&A answer calls
- PR description sync calls
- CI fix analysis and implementation calls
- All calls have the ticket key, task type, and token counts attached

### Trace tags and metadata

You can attach workflow context to every Langfuse trace as tags or metadata, making it easy to filter traces by ticket type, CI status, event source, and so on.

Configure the fields you want via two comma-separated env vars:

```bash
# Fields to attach as searchable Langfuse tags.
# Keep these enabled for Grafana dashboards.
LANGFUSE_TRACE_TAGS=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,llm_model

# Fields to attach as Langfuse trace metadata.
# Grafana ClickHouse queries require project_id, ticket_type, workflow_step, and ticket_key.
LANGFUSE_TRACE_METADATA=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,retry_count,system_prompt_length,llm_model
```

Available field names for both settings:

| Field | Description |
|-------|-------------|
| `ticket_key` | Jira ticket key (e.g. `PROJ-123`) |
| `ticket_type` | Ticket type (`Feature`, `Bug`, etc.) |
| `project_id` | Jira project ID derived from the ticket key |
| `workflow_step` | Name of the workflow node that triggered the call |
| `repo` | GitHub repository (`owner/repo`) |
| `pr_number` | Pull request number |
| `ci_status` | Last known CI status |
| `event_type` | Webhook event type that started the workflow |
| `event_source` | Source system (e.g. `jira`, `github`) |
| `retry_count` | Number of retries so far |
| `system_prompt_length` | Length of the system prompt in characters (metadata only) |
| `llm_model` | Model used for the call |

Invalid or unknown field names are ignored and logged at `WARNING` level on startup.

### Disabling tracing

```bash
LANGFUSE_ENABLED=false
```

---

## 10. Debugging Tools

### Snapshot and restore a workflow checkpoint

Save the full state of a ticket to a file and restore it later — useful for reproducing bugs, testing recovery paths, or resetting after a bad patch:

```bash
# Save current state (snapshot files go to devtools/snapshots/)
uv run python devtools/snapshot_checkpoint.py snapshot AISOS-376
uv run python devtools/snapshot_checkpoint.py snapshot AISOS-376 --label before-ci-fix

# List saved snapshots for a ticket
uv run python devtools/snapshot_checkpoint.py list AISOS-376

# Preview what a restore would change (dry-run, default)
uv run python devtools/snapshot_checkpoint.py restore AISOS-376 \
  devtools/snapshots/AISOS-376_20260505_143201_before-ci-fix.json

# Actually apply the restore
uv run python devtools/snapshot_checkpoint.py restore AISOS-376 \
  devtools/snapshots/AISOS-376_20260505_143201_before-ci-fix.json --apply
```

Snapshot files are saved to `devtools/snapshots/` and are gitignored — they stay local.

---

### Patch a workflow checkpoint

Directly edit Redis state for a ticket — useful when a workflow gets stuck due to a bug or incorrect state:

```bash
uv run python devtools/patch_checkpoint.py <ticket-key> <field=value> [field=value ...]
```

**Examples:**

```bash
# Reset a stuck workflow to ci_evaluator
uv run python devtools/patch_checkpoint.py AISOS-376 \
  current_node=ci_evaluator \
  is_paused=false \
  is_blocked=false \
  last_error=null \
  ci_fix_attempts=0

# Resume at wait_for_ci_gate after patching from escalated state
uv run python devtools/patch_checkpoint.py AISOS-376 \
  current_node=wait_for_ci_gate \
  is_paused=true \
  is_blocked=false \
  last_error=null

# Skip e2e checks for a ticket
uv run python devtools/patch_checkpoint.py AISOS-376 \
  'ci_skipped_checks=["e2e-openstack"]'

# Add new state fields introduced by a code change
uv run python devtools/patch_checkpoint.py AISOS-376 \
  'ci_skipped_checks=[]' \
  'review_comments=[]'
```

**JSON parsing:** values are parsed as JSON if possible, otherwise as strings. Use quotes for lists and booleans: `true`/`false`/`null` are parsed correctly.

### Common checkpoint patches

| Situation | Patch |
|-----------|-------|
| Workflow wrongly escalated to blocked | `current_node=ci_evaluator is_blocked=false last_error=null ci_fix_attempts=0` |
| Restart CI from scratch | `current_node=wait_for_ci_gate is_paused=true ci_fix_attempts=0` |
| Skip a flaky CI check | `'ci_skipped_checks=["check-name-substring"]'` |
| New field added in code | `new_field=default_value` |
| Force retry after fix | add `forge:retry` label in Jira instead |

### forge:retry label

Add the `forge:retry` label to a Jira ticket to resume a blocked workflow. Forge will:
- Clear `last_error`
- Clear `is_blocked`
- Reset `ci_fix_attempts` to 0
- Resume from the node that failed

### Worker logs

The worker logs to stdout. Useful log entries to grep for:

```bash
# Watch for a specific ticket
uv run forge worker 2>&1 | grep AISOS-376

# See what signals are detected
uv run forge worker 2>&1 | grep "Detected"

# See CI evaluation results
uv run forge worker 2>&1 | grep "CI"
```

### Check Redis state directly

```bash
# Connect to Redis
redis-cli -p 6380

# List all checkpoints
KEYS checkpoint:*

# Get a specific checkpoint (LangGraph stores them as JSON)
GET checkpoint:AISOS-376:...
```

---

## 11. Common Workflows

### Start a new feature end-to-end (local test)

```bash
# 1. Make sure worker and API are running
docker compose up redis forge-api -d
uv run forge worker &

# 2. Create the feature
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/01-feature-created.json

# 3. Watch worker logs, approve each stage as it completes
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/03-prd-approved.json
# ... and so on through task approval
```

### Test CI gate skip

```bash
# 1. Get a PR with failing CI checks
gh pr checks 42 --repo org/repo

# 2. Patch the checkpoint to wait_for_ci_gate if not already there
uv run python devtools/patch_checkpoint.py TICKET-123 \
  current_node=wait_for_ci_gate is_paused=true ci_fix_attempts=0

# 3. Send the skip-gate command
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issue_comment" \
  -d '{
    "action": "created",
    "issue": {
      "number": 42,
      "title": "[TICKET-123] Title",
      "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/42"}
    },
    "comment": {"body": "/forge skip-gate failing-check-name"},
    "repository": {"full_name": "org/repo"},
    "sender": {"login": "you"}
  }'

# 4. Send a CI webhook to trigger re-evaluation
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: check_run" \
  -d '{"action":"completed","check_run":{"status":"completed","conclusion":"failure","head_sha":"SHA","pull_requests":[{"number":42,"head":{"ref":"forge/ticket-123"}}]},"repository":{"full_name":"org/repo"},"sender":{"login":"you"}}'
```

### Trigger the human review flow

```bash
# After CI passes, the workflow pauses at human_review_gate.
# Send a review webhook to trigger implement_review:
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request_review" \
  -d '{
    "action": "submitted",
    "review": {
      "state": "commented",
      "body": "Please fix the jitter constant comment in controller.go.",
      "commit_id": "YOUR_HEAD_SHA"
    },
    "pull_request": {
      "number": 42,
      "title": "[TICKET-123] Title",
      "head": {"ref": "forge/ticket-123", "sha": "YOUR_HEAD_SHA"},
      "state": "open"
    },
    "repository": {"full_name": "org/repo"},
    "sender": {"login": "you"}
  }'
```

---

## 12. Service Reference

### Ports

| Service | Port | URL |
|---------|------|-----|
| Forge API | 8000 | `http://localhost:8000` |
| API metrics | 8000 | `http://localhost:8000/metrics` |
| Worker metrics | 8001 | `http://localhost:8001/metrics` |
| Redis | 6380 | `redis://localhost:6380/0` |
| Prometheus | 9092 | `http://localhost:9092` |
| Grafana | 3010 | `http://localhost:3010` |

### API endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/health` | GET | Health check |
| `/api/v1/webhooks/jira` | POST | Jira webhook receiver |
| `/api/v1/webhooks/github` | POST | GitHub webhook receiver |
| `/metrics` | GET | Prometheus metrics |

### GitHub PR comment commands

| Command | Effect | Active at |
|---------|--------|-----------|
| `/forge skip-gate <name>` | Skip named CI check | CI stages |
| `/forge unskip-gate <name>` | Remove a skip | CI stages |

### Jira labels

| Label | Meaning |
|-------|---------|
| `forge:managed` | Ticket managed by Forge |
| `forge:prd-pending` | Awaiting PRD approval |
| `forge:prd-approved` | PRD approved |
| `forge:spec-pending` | Awaiting spec approval |
| `forge:spec-approved` | Spec approved |
| `forge:plan-pending` | Awaiting epic plan approval |
| `forge:plan-approved` | Plan approved |
| `forge:task-pending` | Awaiting task approval |
| `forge:task-approved` | Tasks approved, implementation starts |
| `forge:blocked` | Workflow blocked, needs intervention |
| `forge:retry` | Resume a blocked workflow |
| `forge:yolo` | Autonomous mode — skip all artifact approval gates (⚠️ use with care, see README) |

### Useful `.env` knobs for development

```bash
LOG_LEVEL=DEBUG              # verbose logging
CONTAINER_LANGCHAIN_VERBOSE=true  # verbose container agent logs
LANGFUSE_ENABLED=false       # disable tracing for speed
JIRA_WEBHOOK_SECRET=         # skip signature validation
GITHUB_WEBHOOK_SECRET=       # skip signature validation
CI_FIX_MAX_RETRIES=1         # fail fast during CI testing

# ⚠️ Dev only — fall back to env vars instead of requiring Jira project properties
FORGE_REQUIRE_PROJECT_CONFIG=false
GITHUB_KNOWN_REPOS=acme/backend,acme/frontend
GITHUB_DEFAULT_REPO=acme/backend
```
