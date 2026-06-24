<p align="center">
  <img src="docs/images/logo.png" alt="Forge Logo" width="1000">
</p>

# Forge - AI-Integrated SDLC Orchestrator

Forge automates the software development lifecycle from Feature ideation through code delivery using AI-powered planning and execution. It connects Jira, GitHub, and Claude to transform tickets into shipped code with human approval gates at each stage.

## How It Works

Forge listens for Jira and github webhooks and orchestrates a multi-stage workflow:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            FEATURE WORKFLOW                                   │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │  Create  │──>│ Generate │──>│ Generate │──>│ Decompose│                 │
│  │  Feature │   │   PRD    │   │   Spec   │   │  Epics   │                 │
│  └──────────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘                 │
│                      │              │              │                         │
│                 [Approval]     [Approval]     [Approval]                    │
│                   ↕ Q&A          ↕ Q&A          ↕ Q&A                       │
│                      │              │              │                         │
│                      v              v              v                         │
│                                                                               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐ │
│  │ Generate │──>│Implement │──>│  Local   │──>│  Create  │──>│  CI/CD   │ │
│  │  Tasks   │   │   Code   │   │  Review  │   │    PR    │   │  + Fix   │ │
│  └────┬─────┘   └──────────┘   └──────────┘   └──────────┘   └────┬─────┘ │
│       │                                                             │        │
│  [Approval]                                                   [AI Review]   │
│    ↕ Q&A                                                           │        │
│       │                                                             v        │
│       v                                                       ┌──────────┐  │
│                                                               │  Human   │──>│
│                                                               │  Review  │   │
│                                                               └──────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘

Comments: ? = question, ! = revision, no prefix = ignored
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Redis Stack (includes RediSearch module)
- Podman (for code execution containers)
- Jira Cloud account with API access
- GitHub account with Personal Access Token
- Anthropic API key (or Google Vertex AI)

### 2. Installation

```bash
# Clone and install
git clone https://github.com/your-org/forge.git
cd forge
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your credentials (see Configuration section)

# Build the container image
podman build -t forge-dev:latest -f containers/Containerfile containers/
```

### 3. Start Services

```bash
# Terminal 1 — Redis (the only service that runs in Docker)
docker compose up redis -d

# Terminal 2 — API server
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0

# Terminal 3 — Worker (must run on the host — it spawns Podman containers)
uv run forge worker
```

### 4. Configure Webhooks

Set up webhooks in Jira and GitHub pointing to your server:

**Jira Webhook:**
- URL: `https://your-server.com/api/v1/webhooks/jira`
- Events: Issue created, updated, commented

**GitHub Webhook:**
- URL: `https://your-server.com/api/v1/webhooks/github`
- Events: Pull requests, Pull request reviews, Check runs, Issue comments


## Usage

### Starting a Feature Workflow

1. **Create a Jira Feature** with the label `forge:managed`
2. Forge automatically generates a PRD and posts it to the ticket
3. **Review and approve** by changing the label to `forge:prd-approved`
4. Forge generates a behavioral specification
5. Continue approving through Spec → Epics → Tasks → Implementation

### Workflow Labels

Use these labels in Jira to control the workflow:

| Stage | Pending Label | Approved Label |
|-------|--------------|----------------|
| PRD | `forge:prd-pending` | `forge:prd-approved` |
| Spec | `forge:spec-pending` | `forge:spec-approved` |
| Plan | `forge:plan-pending` | `forge:plan-approved` |
| Tasks | `forge:task-pending` | `forge:task-approved` |

### Autonomous Mode (`forge:yolo`)

> **⚠️ Warning:** Adding `forge:yolo` to a ticket removes all human approval checkpoints for planning artifacts. Forge will proceed from ticket creation straight through to implementation without pausing at the PRD, spec, plan, or task gates. Use this only when you trust the requirements and are comfortable with Forge making all planning decisions autonomously.

Add `forge:yolo` to a ticket to enable autonomous mode:
- Forge skips the PRD, spec, plan, and task approval gates
- In the bug workflow, Forge auto-selects RCA option 1
- **The code review gate is never skipped** — a human reviewer is always required on the implementation PR
- `forge:yolo` can be added at ticket creation or while the workflow is already paused at a gate — Forge will immediately advance

### Jira Comment Syntax

Forge classifies Jira comments by their prefix:

| Prefix | Type | What happens |
|--------|------|--------------|
| `!` | Revision request | Forge regenerates the current artifact with your feedback |
| `?` or `@forge ask` | Question | Forge answers without advancing or regenerating |
| `>option N` | RCA option selection | Selects a fix option (RCA Option Gate only) |
| _(no prefix)_ | Informational | Ignored by the workflow |

### Requesting Revisions

Start your comment with `!` followed by your feedback. Forge will regenerate the current artifact incorporating your feedback.

```
! The PRD is missing non-functional requirements for latency
```

### Asking Questions (Q&A Mode)

While reviewing a PRD or Spec, you can ask clarifying questions without triggering regeneration:

- Start your comment with `?` — e.g., `? Why did you choose REST over GraphQL?`
- Or use `@forge ask` — e.g., `@forge ask explain the auth approach`

Forge will answer based on the artifact content and generation context, then keep the workflow paused for your approval decision. When you approve, a summary of Q&A exchanges is posted to the ticket for future reference.

!!! note
    Comments without a recognized prefix (`!`, `?`, `@forge ask`, `>option`) are treated as informational and do not trigger any workflow action.

### Handling Failures

When a workflow fails:
1. Forge sets the `forge:blocked` label
2. Forge posts a comment tagging the reporter and assignee
3. To retry: Add the `forge:retry` label — Forge resumes from the exact node that failed, not from the beginning

> **CI-specific:** If CI fix attempts are exhausted, adding `forge:retry` resets the attempt counter so Forge gets a fresh budget of retries.

### Skipping CI Gates

When a CI check fails due to infrastructure issues unrelated to your code (e.g. a cloud environment outage, quota exhaustion, or a flaky test runner), you can bypass it with a PR comment:

```
/forge skip-gate <check-name-substring>
```

**Examples:**
```
/forge skip-gate e2e-openstack-ovn
/forge skip-gate e2e-openstack        ← skips all checks containing this substring
```

Forge will:
1. Reply on the PR confirming the skip
2. Post an audit comment on the Jira ticket
3. Re-evaluate CI treating the skipped check as passing

To remove a skip:
```
/forge unskip-gate e2e-openstack-ovn
```

Skips persist across pushes — if the infra check fails again on the next commit, it is still skipped. The check name is matched as a case-insensitive substring of the full check name.

> **Note:** Certain checks (e.g. `tide`, Prow's merge-queue) are always pending and are permanently ignored. Configure with `CI_IGNORED_CHECKS` in `.env`.

### Resolving Merge Conflicts

When a PR falls behind `main` and develops merge conflicts, post a comment:

```
/forge rebase
```

Forge merges `main` into the PR branch, resolving any conflicts using AI. If the merge is clean, it pushes immediately. If there are conflicts, a container with Claude resolves them using the PR description as context, then force-pushes to the fork. This works from any workflow stage.

See [PR Commands](docs/guide/pr-commands.md) for the full reference.

### Bug Workflow

Bugs follow a five-stage pipeline:

```
Create Bug → Triage → RCA Analysis + Reflection → [RCA Option Gate] → Plan → [Plan Approval] → Decompose → Implement → PR → CI → Review → Post-merge Summary
```

See [Bug Workflow Guide](docs/guide/bug-workflow.md) for the full stage reference.

## Workflow Details

### Feature Workflow Stages

| Stage | What Happens | Human Action |
|-------|--------------|--------------|
| **PRD Generation** | AI transforms ticket description into structured PRD | Review, ask questions (`?`), approve or revise (`!`) |
| **Spec Generation** | AI creates behavioral spec with Given/When/Then criteria | Review, ask questions (`?`), approve or revise (`!`) |
| **Epic Decomposition** | AI breaks feature into logical Epics with plans | Review, ask questions (`?`), approve or revise (`!`) |
| **Task Generation** | AI creates implementation Tasks per repository | Review, ask questions (`?`), approve or revise (`!`) |
| **Implementation** | Code executed in ephemeral Podman containers | (Automatic) |
| **Local Code Review** | Reviews the diff against main, fixes breaking issues in-place (up to 2 passes) before PR creation | (Automatic) |
| **PR Creation** | Fork-based pull request created with AI-generated description; PR body synced against commits | (Automatic) |
| **CI Validation** | Pauses until GitHub CI webhook; on failure: runs two-stage analyze-then-fix pipeline (up to 5 retries). Each fix pass is reviewed in-place before push; PR description synced after each push. Specific checks can be skipped via PR comment. | (Automatic + `/forge skip-gate`) |
| **AI Review** | Reviews the PR against the spec after CI passes | (Automatic) |
| **Human Review** | PR ready for human review | Merge or request changes |

### Bug Workflow Stages

| Stage | What Happens | Human Action |
|-------|--------------|--------------|
| **Triage** | Evaluates ticket against 7-field completeness checklist | Provide missing fields if prompted |
| **RCA Analysis** | Container performs hypothesis-driven codebase exploration; reflection loop validates the output (up to 3 passes) | (Automatic) |
| **RCA Option Gate** | Structured RCA + fix options posted; `forge:rca-pending` set | Reply `>option N` to select approach; or revise with `!` |
| **Planning** | Container produces concrete implementation plan with per-repo `repo:` tags | Approve with `forge:plan-approved`; or revise with `!` |
| **Decompose** | One Jira Task created per repository; tasks linked to bug | (Automatic) |
| **Implementation** | Fix implemented in ephemeral container with TDD + bidirectional test validation; qualitative review (7-item checklist, up to 2 retries) | (Automatic) |
| **PR → CI → Review** | Same as Feature workflow; PR includes release note section | Merge or request changes |
| **Post-merge Summary** | Fix summary + release note posted to Jira ticket | (Automatic) |

## Architecture

Forge is event-driven. Jira and GitHub send webhooks; Forge processes them asynchronously and calls back into Jira and GitHub with the results. Human approval happens through Jira label changes and GitHub PR reviews, which fire new webhooks to resume the workflow.


### Components

**FastAPI Gateway** — Receives webhooks from Jira and GitHub, validates signatures, and enqueues events. Returns immediately; all processing is async.

**Redis Streams** — Durable FIFO queue between the API and the worker. Ensures no event is lost if the worker restarts mid-processing.

**LangGraph Workflow** — State machine that routes each event to the right node (generate PRD, wait for approval, implement task, etc.) and checkpoints state to Redis after every step. Resumable from any point.

**Orchestrator Agent** — A [Deep Agents](https://github.com/deepagents/deepagents) instance that runs on the host. Handles all planning stages: reads the Jira ticket, generates PRD/spec/epics/tasks via Claude or Gemini, posts results back to Jira, and waits for human approval via Jira label changes. Uses Jira and GitHub MCP tools.

**Container Runner + Container Agent** — For implementation tasks, the workflow spawns an ephemeral Podman container. Inside runs a second Deep Agents instance with access to the cloned repository but no external network. It writes code, runs tests, commits, and exits. The host orchestrator then creates the PR.

**Skills** — Markdown files loaded by both agents that define what to produce and how to reason. Resolved per Jira project: `skills/{project}/` overrides `skills/default/` on a per-skill basis. See [`skills/README.md`](skills/README.md).

## Configuration

### Required Environment Variables

```bash
# Jira
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USER_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-jira-api-token

# GitHub
GITHUB_TOKEN=github_pat_your_token

# LLM (choose one)
ANTHROPIC_API_KEY=sk-ant-your-api-key  # Direct Anthropic API
# OR
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project  # Vertex AI
ANTHROPIC_VERTEX_REGION=us-east5

# Model selection
LLM_MODEL=claude-opus-4-5@20251101

# Redis
REDIS_URL=redis://localhost:6380/0
```

### Per-Project Repository Configuration

> **Repository configuration is set per Jira project, not in `.env`.**

Each Jira project Forge manages needs two project properties set by an admin:

```bash
# Available repos for this project (set once per Jira project via REST API)
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.repos" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '["org/repo1", "org/repo2"]'

# Default repo when no explicit assignment is made
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.default_repo" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '"org/repo1"'
```

If these properties are not set, Forge posts a clear configuration error comment on the ticket and blocks the workflow until they are added.

> **Local development shortcut:** Set `FORGE_REQUIRE_PROJECT_CONFIG=false` in `.env` to fall back to `GITHUB_KNOWN_REPOS` / `GITHUB_DEFAULT_REPO` env vars instead of requiring Jira project properties. See the [Developer Guide](docs/developer-guide.md#️-local-development-env-var-fallback-mode) for details.

See `.env.example` for the complete list of configuration options including:
- MCP server configuration
- Container resource limits
- Langfuse observability
- Webhook secrets

### MCP Servers

Forge agents can access external tools via MCP (Model Context Protocol):

| Server | Description |
|--------|-------------|
| `github` | GitHub Copilot MCP for repo operations |
| `atlassian` | Atlassian MCP for Jira/Confluence |
| `context7` | Library documentation lookup |
| `gitmcp` | Repository documentation |

Configure in `mcp-servers.json`. By default, MCP tools are read-only.

## Project Structure

```
src/forge/
├── api/                 # FastAPI routes and middleware
├── integrations/        # Jira, GitHub, Agents, Langfuse clients
├── models/              # Domain models (workflow, events)
├── orchestrator/        # Worker and checkpointing
├── workflow/            # Feature and bug workflow state machines
│   ├── nodes/          # Workflow node implementations
│   └── gates/          # Human-in-the-loop approval gates
├── prompts/v1/          # Versioned system prompt templates
├── queue/               # Redis Streams producer/consumer
├── sandbox/             # Container runner
├── skills/              # Skill resolver (per-project path resolution)
└── workspace/           # Git operations

skills/                  # Agent skill files
├── default/            # Stack-agnostic defaults for all projects
└── {project}/          # Per-project overrides (Jira key, lowercase)

containers/              # Container image and entrypoint
tests/                   # Unit and integration tests
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Health check |
| `/api/v1/webhooks/jira` | POST | Jira webhook receiver |
| `/api/v1/webhooks/github` | POST | GitHub webhook receiver |
| `/metrics` | GET | Prometheus metrics |

## Observability

### Metrics (Prometheus)

- **API server**: `http://localhost:8000/metrics`
- **Worker**: `http://localhost:8001/metrics`
- **Prometheus UI**: `http://localhost:9092`
- **Grafana dashboards**: `http://localhost:3010` when the compose `grafana` service is running

Key metrics:
- `forge_workflows_started_total` - Workflows started by type
- `forge_workflows_completed_total` - Workflows completed
- `forge_ci_fix_attempts_total` - CI fix attempts
- `forge_agent_duration_seconds` - Agent execution time

### Tracing (Langfuse)

All LLM calls are traced to Langfuse when configured:
- PRD/Spec generation traces
- Epic decomposition traces
- Code implementation traces

## Development

```bash
uv run pytest tests/unit/ -v   # run tests
uv run ruff check src/         # lint
uv run ruff format src/        # format
uv run mypy src/forge/         # type check
```

For a full local setup walkthrough, payload-based testing, Prometheus metrics, Langfuse tracing, and debugging tools, see the **[Developer Guide](docs/developer-guide.md)**.

## Documentation

For how to preview, build, and deploy the documentation site, see [DOCS.md](DOCS.md).

## Contributing

The primary way to contribute is to write a skill set for your team's stack — CI tooling, PRD format, implementation conventions — and share it under `skills/{your-project-key}/`. You only override the skills you change; everything else falls back to the defaults automatically.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.
