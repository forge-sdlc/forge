# Testing Skills Locally

Test Forge skills locally using `forge test-skill` — the same deepagents
code path as hosted Forge, without needing Jira, GitHub, or the hosted beta.

## Why

Iterating via the hosted beta costs $3-17 per run and requires real Jira
tickets. `forge test-skill` runs the exact same agent locally against
pre-fetched input, so you can iterate in minutes.

## Prerequisites

- Forge installed from source (`pip install -e .` or `uv sync`)
- `deepagents`, `langchain-anthropic`, `langgraph` (included in Forge's dependencies)
- `ANTHROPIC_API_KEY` set, or `ANTHROPIC_VERTEX_PROJECT_ID` for Vertex AI

## Quick Start

### Run a skill

```bash
forge test-skill run \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --input test-cases/PROJ-1234/input.yaml \
  --output output/PROJ-1234/
```

### Evaluate the output

```bash
forge test-skill eval \
  --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
  --generated output/PROJ-1234/enhancements/PROJ-1234/prd.md \
  --gold test-cases/PROJ-1234/gold-prd.md \
  --output output/PROJ-1234/eval/
```

## How It Works

The runner:

1. Loads the system prompt from `forge.prompts` (same templates as production)
2. Loads the user message from `src/forge/prompts/v1/{skill-name}.md`
3. Creates a temp workspace mimicking the container layout
   (`/opt/forge/skills/{project}/`, `/home/user/`)
4. Creates a deepagents agent with `FilesystemBackend` and `SkillsMiddleware`
5. Invokes the agent and collects output files + trace

This is the same `create_deep_agent()` + `FilesystemBackend` code path
used by `ForgeAgent` in production — not a simulation.

## Preparing Test Input

Create an `input.yaml` with pre-fetched Jira content:

```yaml
jira_key: PROJ-1234
title: "Feature Title"
prompt: |
  # PROJ-1234: Feature Title

  ## Description
  The full Jira feature description goes here.
  Copy it from Jira — no live access needed at runtime.

  ## User Stories
  ...
```

If a `gold-prd.md` file exists alongside `input.yaml`, it's automatically
appended to the prompt as an approved PRD (useful for `generate-spec` testing).

## Adding Reference Documentation

If your skill behavior depends on reference URLs configured in
`forge.references`, export and pass them:

```bash
# Export from Forge project config
forge get-config MYPROJECT --property forge.references > refs.json

# Pass to test runner
forge test-skill run \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --references refs.json \
  --input test-case.yaml \
  --output output/
```

## Adding Repository Context

To give the agent access to codebase files (for skills that read code):

```bash
forge test-skill run \
  --skill generate-spec \
  --skill-dir skills/myproject/generate-spec \
  --project myproject \
  --repos /path/to/myrepo /path/to/docs-repo \
  --input test-case.yaml \
  --output output/
```

Repos are copied into the workspace at `/home/user/{repo-name}/`,
excluding `.git`, `__pycache__`, `node_modules`, `.venv`, and `vendor`.

## MLflow Integration

Track runs and evaluations in MLflow:

```bash
forge test-skill run \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --input test-case.yaml \
  --output output/ \
  --mlflow http://mlflow-host:5000

forge test-skill eval \
  --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
  --dataset eval/dataset/cases/ \
  --results-dir output/ \
  --output output/eval/ \
  --mlflow http://mlflow-host:5000
```

Logged metrics: elapsed time, token counts, cost estimate, iteration count,
and per-criterion eval scores.

## Configuration

`devtools/test-skill/config.yaml`:

```yaml
model: claude-opus-4-6    # override with --model
max_tokens: 16384
project: default           # override with --project
```

## What's Not Simulated

- Shell/command execution (`LocalShellBackend`) — the test runner uses
  `FilesystemBackend`, which provides file read/write/grep but no shell
- MCP tools — not loaded in the test runner
- Jira/GitHub integrations — the runner is offline by design
- Conversation summarization thresholds — may differ from production
