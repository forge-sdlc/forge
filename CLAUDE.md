# Forge Development Guidelines

## Overview

Forge is an AI-powered SDLC orchestrator that automates software development workflows using LangGraph, FastAPI, and Claude.

## Tech Stack

- Python 3.11+ with LangGraph for workflow orchestration
- FastAPI for webhook handling
- Redis for event queuing and state checkpointing
- Anthropic Claude (via direct API or Vertex AI)
- Deep Agents for autonomous code implementation
- Podman for containerized code execution

## Project Structure

```
src/forge/
├── api/                 # FastAPI routes and middleware
├── integrations/        # Jira, GitHub, Agents, Langfuse clients
├── models/              # Domain models (workflow, events, etc.)
├── orchestrator/        # LangGraph workflow nodes and gates
├── prompts/v1/          # Versioned prompt templates
├── queue/               # Redis Streams producer/consumer
├── sandbox/             # Container runner for code execution
├── workspace/           # Git operations and workspace management
└── config.py            # Application configuration

containers/              # Container image and entrypoint
tests/                   # Unit and integration tests
```

## Commands

```bash
# Run tests
uv run pytest

# Run specific tests
uv run pytest tests/unit/ -v

# Linting
uv run ruff check src/

# Format code
uv run ruff format src/

# Type checking
uv run mypy src/forge/

# Start API server (dev)
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0

# Start queue worker
uv run forge worker

# Build container
podman build -t forge-dev:latest containers/
```

## Debugging Container Failures

When a container exits with a non-zero code and the logs are unhelpful (e.g. only showing MCP server startup messages), enable container preservation to inspect the full state:

**1. Set in `.env`:**
```bash
FORGE_CONTAINER_KEEP=true
```

**2. Trigger the failing workflow.** When a container fails, the worker logs will print the container name and ready-to-run commands:
```
Container kept for debugging (FORGE_CONTAINER_KEEP=true): forge-AISOS-678-12345
  Inspect logs:      podman logs forge-AISOS-678-12345
  Enter filesystem:  podman export forge-AISOS-678-12345 | tar -xC /tmp/forge-AISOS-678-12345
  Remove when done:  podman rm forge-AISOS-678-12345
```

**3. Common things to check:**
- `podman logs <name>` — full stdout/stderr from the agent inside the container
- `podman export <name> | tar -xC /tmp/<name>` then inspect `/tmp/<name>/workspace/` and `/tmp/<name>/workspace/.forge/` for any partial outputs
- Check if the container image needs to be rebuilt: `podman build -t forge-dev:latest containers/`

**4. Clean up when done:**
```bash
podman rm forge-AISOS-678-12345
# or remove all stopped forge containers:
podman rm $(podman ps -a --filter name=forge- -q)
```

**Remember:** Set `FORGE_CONTAINER_KEEP=false` (or remove it) before running in production — accumulated stopped containers consume disk space.

## Code Style

- Use `X | None` instead of `Optional[X]` (PEP 604)
- Use `StrEnum` for string enums
- Use `contextlib.suppress()` instead of empty try/except
- Prefix unused parameters with `_`
- Keep functions focused and small

## Workflow Labels

| Label | Meaning |
|-------|---------|
| `forge:managed` | Ticket is managed by Forge |
| `forge:triage-pending` | Bug awaiting triage completion |
| `forge:rca-pending` | Bug awaiting RCA option selection |
| `forge:prd-pending` | Awaiting PRD approval |
| `forge:spec-pending` | Awaiting spec approval |
| `forge:plan-pending` | Awaiting plan approval |
| `forge:task-pending` | Awaiting task approval |
| `forge:task-triage-pending` | Task takeover awaiting triage completion |
| `forge:managed:task` | Task identity preservation label |
| `forge:managed:task-takeover` | Task takeover identity preservation label |
| `forge:blocked` | Workflow blocked, needs intervention |
| `forge:retry` | Trigger retry of failed step |
| `forge:yolo` | Autonomous mode — skip all artifact approval gates (see warning below) |

> **⚠️ Warning — `forge:yolo`:** This label removes all human checkpoints for PRD, spec, plan, task, and task plan approval. Forge will proceed autonomously from ticket creation to implementation without pausing for review. Only use this on tickets where you are confident in the requirements and comfortable with Forge making all planning decisions. It does not bypass code review (the human review gate on the implementation PR is always required).

## Jira Comment Syntax

| Prefix | Effect |
|--------|--------|
| `!` | Revision request — triggers regeneration with feedback |
| `?` or `@forge ask` | Question — triggers Q&A answer |
| `>option N` | RCA option selection (RCA Option Gate only) |
| _(no prefix)_ | Informational — workflow ignores it |

## GitHub PR Comment Commands

| Command | Where | Effect |
|---------|-------|--------|
| `/forge skip-gate <name>` | PR comment | Skip a named CI check (substring match); persists across pushes |
| `/forge unskip-gate <name>` | PR comment | Remove a previously set skip |
| `/forge rebase` | PR comment | Merge main into PR branch, resolving conflicts with AI |

Skip-gate commands are only active at CI stages (`wait_for_ci_gate`, `ci_evaluator`, `attempt_ci_fix`). Rebase works from any workflow stage.

## PRD & Spec Approval via GitHub PR

Opt-in per project via Jira project property. When configured, Forge opens PRs in the proposals repo for PRD and spec review instead of posting to Jira. Reviewer feedback triggers regeneration; merging the PR signals approval.

**Per-project config (Jira project property):**

| Property | Example | Description |
|----------|---------|-------------|
| `forge.prd_proposals_repo` | `org/enhancement-proposals` | Enables PR-based PRD/spec approval for this project |
| `forge.prd_proposals_path` | `enhancements` | Base directory for enhancement folders (default: repo root) |

Set via: `forge project-setup <PROJECT> --prd-proposals-repo owner/repo`
Remove via: `forge project-setup <PROJECT> --prd-proposals-repo ""`
Set path: `forge project-setup <PROJECT> --prd-proposals-path enhancements`
Reset path: `forge project-setup <PROJECT> --prd-proposals-path ""`

**Global fallbacks (`.env`, used when `FORGE_REQUIRE_PROJECT_CONFIG=false`):**

| Setting | Default | Description |
|---------|---------|-------------|
| `PRD_PROPOSALS_REPO` | (empty) | Fallback `owner/repo` for projects without the property |
| `PRD_PROPOSALS_PATH` | (empty) | Base directory for enhancement folders (empty = repo root) |

**File structure per ticket:**
```
{path}/{TICKET}/
  prd.md        # PRD (branch: forge/prd/{ticket-key})
  design.md     # Spec (branch: forge/spec/{ticket-key})
```

Each artifact gets its own branch and PR. Same repo and path config applies to both.

## Container Execution

Tasks are implemented in ephemeral Podman containers:
- System prompt loaded from `src/forge/prompts/v1/container-system.md`
- Task file written to `.forge/task.json` (excluded from commits)
- Agent has full tool access via Deep Agents
- Changes committed locally, orchestrator handles push/PR
