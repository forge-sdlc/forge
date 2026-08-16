# Configuration

All configuration is via environment variables in `.env`. See `.env.example` in the repository for the complete list with comments.

## Required Variables

### Jira

| Variable | Description |
|----------|-------------|
| `JIRA_BASE_URL` | Your Atlassian instance URL (e.g., `https://your-org.atlassian.net`) |
| `JIRA_USER_EMAIL` | Service account email |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_WEBHOOK_SECRET` | Secret for validating Jira webhook signatures |

### GitHub

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | Personal Access Token with `repo` and `read:org` scopes |
| `GITHUB_WEBHOOK_SECRET` | Secret for validating GitHub webhook signatures |

### LLM

Choose one backend explicitly. Gemini 3.5 Flash through Vertex AI is recommended.

=== "Vertex AI (recommended)"

    ```bash
    LLM_BACKEND=vertex-ai
    GOOGLE_CLOUD_PROJECT=your-gcp-project
    GOOGLE_CLOUD_LOCATION=global
    LLM_MODEL=gemini-3.5-flash
    ```

=== "Gemini API"

    ```bash
    LLM_BACKEND=google-genai
    GOOGLE_API_KEY=your-google-api-key
    LLM_MODEL=gemini-3.5-flash
    ```

=== "Anthropic API"

    ```bash
    LLM_BACKEND=anthropic
    ANTHROPIC_API_KEY=your-anthropic-api-key
    LLM_MODEL=claude-sonnet-4-6
    ```

`LLM_BACKEND` and `LLM_MODEL` are required for legacy configuration. A complete
`MODEL_CONNECTIONS` plus `MODEL_DEFAULT` configuration replaces them and
derives the runtime backend, model, Vertex project, and location. Provider
credentials must use the provider-native variables shown above; legacy aliases
are not supported. Forge validates the backend, credentials, and model
compatibility at startup.

The legacy `CONTAINER_LLM_MODEL` override remains supported. For exact
per-stage selection, administrators can define JSON `MODEL_CONNECTIONS`,
`MODEL_DEFAULT`, and `MODEL_POLICY` values. Connections contain a backend,
provider location, model allowlist, and declared capabilities—never a credential value. Each backend
uses its existing provider-native environment credential. Jira projects then
set `forge.model_policy`, restricted to those connections and models:

```bash
MODEL_CONNECTIONS={"vertex-global":{"backend":"vertex-ai","project":"my-gcp-project","location":"global","allowed_models":["gemini-3.5-flash","claude-sonnet-5"],"capabilities":["tools"]}}
MODEL_DEFAULT={"connection":"vertex-global","model":"gemini-3.5-flash"}
MODEL_POLICY={"generate_prd":{"connection":"vertex-global","model":"claude-sonnet-5"},"generate_spec":{"connection":"vertex-global","model":"gemini-3.5-flash"}}
```

```bash
forge project-setup MYPROJ \
  --model generate_prd=vertex-production:gemini-3.5-pro \
  --model implement_task=anthropic-production:claude-sonnet-4-6

# Set a separate project-wide fallback (individual --model overrides still win)
forge project-setup MYPROJ \
  --model-all vertex-production:gemini-3.5-pro

# Remove one stage override and preserve the rest
forge project-setup MYPROJ --remove-model generate_prd

# Delete all per-stage overrides
forge project-setup MYPROJ --clear-model-policy

# Delete the project-wide fallback
forge project-setup MYPROJ --clear-model-default

# Validate against the local runtime configuration and print every target
forge get-config MYPROJ --models
```

Project overrides require administrators to configure `MODEL_CONNECTIONS`.
The user-facing `project-setup` command validates policy syntax and canonical
stage names but does not require access to the Forge deployment's connection
registry. Forge validates connection names, model allowlists, backends, and
capabilities when it executes a stage. Invalid runtime policy fails closed and
reports the available configured connections and models to Jira and, when an
active PR exists, GitHub. The implicit legacy connection remains restricted to
`LLM_MODEL` and `CONTAINER_LLM_MODEL` and is never exposed to Jira project
policy.

Canonical policy keys are deliberately specific; runtime prompt, skill, and
graph-node names are not accepted in Jira configuration:

| Policy key | Execution |
|---|---|
| `generate_prd` | Initial PRD generation and every PRD revision |
| `generate_spec` | Initial specification generation and every specification revision |
| `decompose_epics` | Epic decomposition or revision |
| `generate_tasks` | Task generation or revision |
| `bug_triage` | Bug-report completeness triage |
| `automated_review_triage` | Classification of automated review feedback |
| `proposal_review_triage` | Classification of proposal review threads |
| `task_takeover_triage` | Existing-task takeover triage |
| `task_takeover_planning` | Existing-task implementation planning |
| `task_takeover_execution` | Existing-task container implementation |
| `task_takeover_review` | Existing-task qualitative review |
| `task_takeover_question` | Questions about task-takeover artifacts |
| `analyze_bug` | Root-cause analysis |
| `reflect_rca` | Root-cause analysis reflection |
| `plan_bug_fix` | Bug-fix planning |
| `implement_bug_fix` | Bug-fix container implementation |
| `implement_task` | Feature-task container implementation |
| `bug_local_review` | Local qualitative review of a bug fix |
| `local_code_review` | Local feature code review |
| `code_review` | Pull-request code review |
| `implement_review_analysis` | Analysis of implementation review feedback |
| `implement_review_fix` | Applying implementation review fixes |
| `generate_pr_description` | Initial pull-request description generation |
| `sync_pr_description` | Pull-request description synchronization |
| `ci_analysis` | CI failure analysis |
| `ci_fix` | CI failure remediation |
| `answer_question` | Q&A about PRDs, specifications, plans, and other generated artifacts |
| `rebase` | Container-assisted rebase conflict resolution |
| `update_docs` | Documentation update generation |

Unknown keys fail validation instead of silently using the default target.

### Keeping artifact generation, revision, and Q&A on one model

Revisions reuse the artifact's generation policy key. For example, both initial
PRD generation and later `!` revision requests resolve `generate_prd`. Questions
submitted with `?` do not regenerate the artifact and resolve the separate
`answer_question` key. Configure both keys when the answer must use the same
model that created the PRD:

```bash
MODEL_POLICY={"generate_prd":{"connection":"vertex-global","model":"claude-opus-4-6"},"answer_question":{"connection":"vertex-global","model":"claude-opus-4-6"}}

forge project-setup MYPROJ \
  --model generate_prd=vertex-global:claude-opus-4-6 \
  --model answer_question=vertex-global:claude-opus-4-6
```

The same pattern applies to `generate_spec`, `decompose_epics`, and
`generate_tasks`: revisions reuse their generation key, while Q&A uses
`answer_question`. Task-takeover artifact questions are the exception and use
`task_takeover_question`.

### Related actions that use separate policy keys

Some user-visible flows contain multiple model invocations with intentionally
different responsibilities. Forge resolves each invocation independently, so
they may use different models unless every related key is mapped to the same
target:

| Flow | Policy keys | Boundary |
|---|---|---|
| Generated artifact interaction | `generate_prd`, `generate_spec`, `decompose_epics`, or `generate_tasks` + `answer_question` | Creation and revision use the generation key; Q&A uses `answer_question` |
| Task takeover | `task_takeover_planning` + `task_takeover_question` | Plan creation and revision are separate from artifact Q&A |
| Automated artifact review | `automated_review_triage` + the relevant generation key | Feedback classification occurs before an accepted revision is generated |
| Proposal review | `proposal_review_triage` + the relevant generation key | Review-thread classification occurs before an accepted revision is generated |
| Implementation review | `implement_review_analysis` + `implement_review_fix` | Feedback analysis and code modification are separate invocations |
| CI remediation | `ci_analysis` + `ci_fix` | Failure diagnosis and code modification are separate invocations |
| Bug investigation | `analyze_bug` + `reflect_rca` + `plan_bug_fix` | Initial analysis, reflection, and repair planning are separate stages |
| Pull-request maintenance | `code_review` + `sync_pr_description` | Code review and description synchronization are separate tasks |

Using different models for these keys is supported and can optimize cost or
quality. When context continuity or consistent judgment is more important,
assign all keys in the flow to the same connection and model. Run
`forge get-config MYPROJ --models` to display the effective target for every
key before testing a workflow.

Forge owns stage capability requirements. Every stage requires a connection
declaring `"capabilities": ["tools"]` except the explicitly tool-free text or
classification stages `automated_review_triage`, `proposal_review_triage`,
`generate_pr_description`, and `sync_pr_description`. Jira policy cannot
weaken that requirement.
Per-target `max_output_tokens` is limited to 131072.

Resolution is project stage override (`forge.model_policy`), then the separate
project-wide fallback (`forge.model_default`), then the deployment stage mapping
(`MODEL_POLICY`), then the deployment default (`MODEL_DEFAULT`). Project policy
does not use a `*` key. `--model-all` writes `forge.model_default` and does not
modify per-stage overrides. When `--model-policy` and `--model` are combined,
individual `--model` entries overwrite matching JSON keys. Used without
`--model-policy`, `--model` preserves the project's other existing stage
overrides; `--model-policy` deliberately replaces the full stage property.
`--remove-model` removes only the named stage and deletes `forge.model_policy`
when no overrides remain. `--clear-model-policy` deletes all per-stage
overrides, while `--clear-model-default` independently deletes the project-wide
fallback.
When `MODEL_CONNECTIONS` is configured, Forge fetches the Jira project policy
before each host or container agent execution, so changes apply automatically
to the next stage or retry. The target remains fixed during that execution's
internal model/tool loop. Legacy and global-only configurations make no extra
Jira policy request. Projects without `forge.model_policy` fall back to the
global stage mapping, then the global default, and finally the legacy
`LLM_BACKEND`/`LLM_MODEL` settings.

### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6380/0` | Redis connection URL |

## Per-Project Repository Configuration

!!! warning "Production requirement"
    In production, Forge reads repository configuration from Jira project properties, **not** from environment variables. If not configured, Forge blocks the workflow and posts setup instructions on the ticket.

Set these properties per Jira project via the REST API:

```bash
# Available repos for this project
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.repos" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '["org/repo1", "org/repo2"]'

# Alternatively, configure a repository with additional metadata (like enabling draft PRs) using an object:
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.repos" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '[
    "org/repo1",
    {
      "name": "org/repo2",
      "draft": true
    }
  ]'

# Default repo when no explicit assignment is made
curl -X PUT \
  "https://your-org.atlassian.net/rest/api/3/project/MYPROJ/properties/forge.default_repo" \
  -H "Content-Type: application/json" \
  -u "you@example.com:YOUR_API_TOKEN" \
  -d '"org/repo1"'
```

## Local Development Overrides

Use these to skip the Jira project property requirement during local development:

| Variable | Description |
|----------|-------------|
| `FORGE_REQUIRE_PROJECT_CONFIG` | Set to `false` to use env var fallbacks instead of Jira project properties |
| `GITHUB_DEFAULT_REPO` | Default repo (`org/repo`) when `FORGE_REQUIRE_PROJECT_CONFIG=false` |
| `GITHUB_KNOWN_REPOS` | Comma-separated list of known repos |

## CI and Validation

| Variable | Description |
|----------|-------------|
| `CI_IGNORED_CHECKS` | Comma-separated list of check name substrings to permanently ignore (e.g., `tide,queue`) |
| `CI_MAX_FIX_ATTEMPTS` | Maximum CI fix attempts before blocking (default: `5`) |

## Container Execution

| Variable | Description |
|----------|-------------|
| `CONTAINER_IMAGE` | Container image for task execution (default: `forge-dev:latest`) |
| `CONTAINER_MEMORY_LIMIT` | Memory limit for task containers (default: `4g`) |
| `CONTAINER_CPU_LIMIT` | CPU limit for task containers (default: `2`) |

## Auto-Review

Settings for the automatic review loop that runs after skill execution. See the [Auto-Review Guide](../guide/auto-review.md) for details.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTO_REVIEW_MAX_RETRIES` | `3` | Default maximum retry attempts when a skill's `review.md` doesn't specify `max_retries` |
| `AUTO_REVIEW_POLL_INTERVAL` | `5.0` | Polling interval in seconds for detecting review cycle files during container execution |
| `AUTO_REVIEW_RECORD_POLLED_FILES` | (none) | Recording mode for polled review cycle files: `log` logs cycle data at INFO level, `copy` copies files to recording directory |

## Observability

### Langfuse Tracing

| Variable | Description |
|----------|-------------|
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `LANGFUSE_HOST` | Langfuse host (defaults to cloud; set for self-hosted) |
| `LANGFUSE_TRACE_TAGS` | Comma-separated list of trace attributes to attach as Langfuse tags. Available values: `ticket_key`, `ticket_type`, `project_id`, `workflow_step`, `repo`, `pr_number`, `ci_status`, `event_source`, `event_type`, `llm_model`. Default: empty (no tags). |
| `LANGFUSE_TRACE_METADATA` | Comma-separated list of trace attributes to attach as Langfuse metadata. Available values: same as tags plus `retry_count`, `system_prompt_length`. Default: empty (no metadata). |

### Grafana Dashboards

These variables are used by `docker-compose.yml`, `devtools/docker-compose.dev.yml`, and `devtools/grafana/compose.grafana.yml`.

| Variable | Description |
|----------|-------------|
| `GRAFANA_PORT` | Host port for Grafana (default: `3010`) |
| `GRAFANA_ADMIN_USER` | Grafana admin user (default: `admin`) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password (default: `grafana`) |
| `LANGFUSE_DOCKER_NETWORK` | External Docker/Podman network for self-hosted Langfuse when using `devtools/grafana/compose.langfuse-network.yml` (default: `langfuse_default`) |
| `CLICKHOUSE_HOST` | Langfuse ClickHouse host reachable from the Grafana container |
| `CLICKHOUSE_PORT` | Langfuse ClickHouse native protocol port (default: `9000`) |
| `CLICKHOUSE_DATABASE` | Langfuse ClickHouse database (default: `default`) |
| `CLICKHOUSE_USER` | Langfuse ClickHouse user |
| `CLICKHOUSE_PASSWORD` | Langfuse ClickHouse password |
| `PROMETHEUS_HOST` | Prometheus host for standalone Grafana compose |
| `PROMETHEUS_PORT` | Prometheus port for standalone Grafana compose |
| `REDIS_HOST` | Redis host for standalone Grafana compose |
| `REDIS_PORT` | Redis port for standalone Grafana compose |

### MCP Servers

MCP server configuration lives in `mcp-servers.json`, not `.env`. See the [MCP servers section](https://github.com/forge-sdlc/forge/blob/main/mcp-servers.json) of the repository.

## test-skill Commands

Local skill testing and evaluation. See [Testing Skills Locally](../dev/test-skill.md) for the full guide.

### forge test-skill run

Run a skill against test cases using deepagents.

| Flag | Required | Description |
|------|----------|-------------|
| `--skill NAME` | Yes | Skill name (e.g., `generate-prd`) |
| `--skill-dir PATH` | Yes | Path to skill directory containing `SKILL.md` |
| `--output DIR` | Yes | Output directory for results and trace |
| `--input FILE` | One of input/dataset | Single `input.yaml` test case |
| `--dataset DIR` | One of input/dataset | Directory of test cases (runs all) |
| `--project NAME` | No | Project name for skill path (overrides `config.yaml`) |
| `--model MODEL` | No | Override model (default: from `config.yaml`) |
| `--references FILE` | No | JSON file with reference docs (same format as `forge.references`) |
| `--repos DIR [DIR...]` | No | Local repo directories to copy into workspace |
| `--mlflow URI` | No | MLflow tracking URI for auto-tracing |
| `--mlflow-experiment NAME` | No | MLflow experiment name (default: `forge-skill-eval`) |

### forge test-skill eval

Evaluate skill outputs against gold standards. See [Skill Evaluation](../dev/skill-evaluation.md) for criteria format.

| Flag | Required | Description |
|------|----------|-------------|
| `--criteria FILE` | Yes | Path to criteria YAML file |
| `--output DIR` | Yes | Output directory for reports |
| `--generated FILE` | Single mode | Path to generated artifact |
| `--gold FILE` | Single mode | Path to gold standard artifact |
| `--dataset DIR` | Batch mode | Dataset directory |
| `--results-dir DIR` | Batch mode | Runner output directory |
| `--mlflow URI` | No | MLflow tracking URI |
| `--mlflow-experiment NAME` | No | MLflow experiment name |
