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
available_repos: ["owner/repository"]
prompt: |
  # PROJ-1234: Feature Title

  ## Description
  The full Jira feature description goes here.
  Copy it from Jira — no live access needed at runtime.

  ## User Stories
  ...
```

For `generate-spec`, an adjacent `gold-prd.md` supplies the approved PRD. For other
skills it is evaluation data and is not appended to the task input.

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

## Fixture and response contracts

The runner uses each stage's production template and typed response schema.
`prompt` is the raw requirements for PRD generation, the approved PRD for spec
generation, the specification for Epic decomposition, and the Epic plan for task
generation. Planning fixtures must list exact `available_repos` names.

Provide additional template inputs under `prompt_inputs`, for example:

```yaml
available_repos: ["owner/repository"]
prompt: |
  The complete approved Epic plan, including repository scope.
prompt_inputs:
  spec_content: "The approved behavioral specification"
  sibling_epics_section: "None"
  existing_tasks_section: "None"
```

Missing required inputs fail before model invocation. Literal braces in input
remain unchanged. Structured responses are validated and saved as `response.json`;
PRD/spec Markdown is also saved separately for evaluation. The runner still uses
a filesystem backend: it does not reproduce shell-capable container stages or
production tool access. Use container regression tests for permission and commit
behavior, not a claim that an offline document evaluation covers them.

The default PRD rubric is `evaluators/criteria/generate-prd.yaml`. OSAC's personas
and template rules live separately in `osac-generate-prd.yaml`.

Compare changes on the same cases: docs-only work, a small feature, multiple
repositories, unavailable access, unknown RCA history, stale CI plans, mixed
review dispositions, and partially updated docs. Track contract validity,
requirement coverage, unsupported claims, false rejection, and side effects along
with tokens, tool calls, elapsed time, and revision count. Shorter instructions
alone do not demonstrate better efficiency.
