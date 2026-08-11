# forge test-skill — Local Skill Testing

Test Forge skills locally without Jira, GitHub, or the hosted beta.
Uses Forge's own deepagents + FilesystemBackend — the same code path
as hosted Forge planning agents.

## Quick Start

```bash
# Via forge CLI
forge test-skill run \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --input test-case.yaml \
  --output output/

# Or directly
python3 devtools/test-skill/run.py \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --input test-case.yaml \
  --output output/
```

## What It Reproduces

Uses Forge's own deepagents library — the same agent, backend, and
middleware as hosted Forge. Skills are discovered via SkillsMiddleware,
not manually injected.

| Component | How |
|-----------|-----|
| Agent | `create_deep_agent()` — same as `ForgeAgent._create_agent_async()` |
| Backend | `FilesystemBackend(virtual_mode=True)` — file tools match production |
| Skills | SkillsMiddleware auto-discovers from `/opt/forge/skills/{project}/` |
| System prompt | `forge.prompts.load_prompt("system")` — same templates as production |
| User message | `load_prompt("{skill-name}")` — same per-skill templates |
| Model | Configurable in `config.yaml` or `--model` flag |
| References | Injected via `--references` (same format as `forge.references` property) |

**Not simulated:** shell/command execution (`LocalShellBackend`), MCP tools,
Jira/GitHub integrations, conversation summarization thresholds.

## Input Format

```yaml
jira_key: PROJ-1234
title: "Feature Title"
prompt: |
  # PROJ-1234: Feature Title

  ## Description
  The full Jira feature description goes here.
  Copy it from Jira — no live access needed at runtime.
```

If a `gold-prd.md` file exists alongside `input.yaml`, it's automatically
appended to the prompt as an approved PRD (useful for generate-spec).

## CLI Reference

### forge test-skill run

| Flag | Description |
|------|-------------|
| `--skill NAME` | Skill name, e.g., `generate-prd` (required) |
| `--skill-dir PATH` | Path to skill directory containing SKILL.md (required) |
| `--input FILE` | Single input.yaml test case |
| `--dataset DIR` | Directory of test cases (runs all) |
| `--output DIR` | Output directory (required) |
| `--project NAME` | Project name for skill path (overrides config.yaml) |
| `--model MODEL` | Override model (default: `claude-opus-4-6`) |
| `--references FILE` | JSON file with reference docs (same format as `forge.references`) |
| `--repos DIR [DIR...]` | Local repo directories to copy into workspace |
| `--mlflow URI` | MLflow tracking URI for auto-tracing |
| `--mlflow-experiment NAME` | MLflow experiment name (default: `forge-skill-eval`) |

### forge test-skill eval

| Flag | Description |
|------|-------------|
| `--criteria FILE` | Path to criteria YAML (required) |
| `--generated FILE` | Path to generated artifact |
| `--gold FILE` | Path to gold standard artifact |
| `--dataset DIR` | Dataset directory (batch mode) |
| `--results-dir DIR` | Runner output directory (batch mode) |
| `--output DIR` | Output directory for reports (required) |
| `--mlflow URI` | MLflow tracking URI |

## Evaluator

Judges generated artifacts against gold standards using an LLM judge
(Sonnet by default). Criteria are defined per skill in YAML:

```yaml
# evaluators/criteria/generate-prd.yaml
skill: generate-prd
judge_model: claude-sonnet-4-6
gold_standard_file: gold-prd.md

criteria:
  - id: scope-accuracy
    name: "Scope Accuracy"
    weight: critical
    prompt: |
      Compare In Scope and Out of Scope items against the gold standard...
```

Reports: terminal (colored), JSON (`results.json`), HTML (`report.html`).

## Adding a New Skill

1. Verify the prompt template exists at `src/forge/prompts/v1/{skill-name}.md`
2. Create `input.yaml` with pre-fetched Jira content
3. Point `--skill-dir` to the skill directory (must contain `SKILL.md`)
4. Run it — output files and `trace.json` go to `--output`
5. Optionally create `evaluators/criteria/{skill-name}.yaml` for automated grading

## References

To inject reference documentation (matching `forge.references` project property):

```bash
# Export from Forge config
forge get-config MYPROJECT --property forge.references > refs.json

# Use in test runner
forge test-skill run \
  --skill generate-prd \
  --skill-dir skills/myproject/generate-prd \
  --project myproject \
  --references refs.json \
  --input test-case.yaml \
  --output output/
```

## Configuration

`devtools/test-skill/config.yaml`:

```yaml
model: claude-opus-4-6
max_tokens: 16384
project: default        # override with --project
```

## Requirements

Requires `deepagents`, `langchain-anthropic`, and `langgraph` (all in
Forge's `pyproject.toml`). Uses Vertex AI when `ANTHROPIC_VERTEX_PROJECT_ID`
is set, otherwise direct Anthropic API.

## Related

- PR: https://github.com/forge-sdlc/forge/pull/297
- Issue: https://github.com/forge-sdlc/forge/issues/296
