# Forge Grafana Dashboards

Grafana provisioning for Forge observability dashboards. The stack is
preconfigured for Langfuse ClickHouse, Forge Prometheus, and Forge Redis.

## Start

When running the full Forge compose stack:

```bash
docker compose --env-file .env \
  -f docker-compose.yml \
  -f devtools/grafana/compose.langfuse-network.yml \
  up -d grafana
```

When running Forge services locally on the host with the devtools stack:

```bash
docker compose --env-file .env \
  -f devtools/docker-compose.dev.yml \
  -f devtools/grafana/compose.langfuse-network.yml \
  up -d
```

For dashboard-only development against already-running Prometheus/Redis/ClickHouse:

```bash
docker compose --env-file .env \
  -f devtools/grafana/compose.grafana.yml \
  -f devtools/grafana/compose.langfuse-network.yml \
  up -d
```

UI (default port): <http://localhost:3010> - log in as **admin / grafana**.

## Required Forge Trace Fields

The dashboards use Forge's configurable Langfuse trace fields for filters and
workflow-step breakdowns. Configure these in `.env` before running Forge:

```bash
LANGFUSE_TRACE_TAGS=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,llm_model
LANGFUSE_TRACE_METADATA=ticket_key,ticket_type,project_id,workflow_step,repo,pr_number,ci_status,event_source,event_type,retry_count,system_prompt_length,llm_model
```

## Local Self-Hosted Langfuse

The default ClickHouse settings in `.env.example` match the standard local
Langfuse compose stack:

```bash
LANGFUSE_DOCKER_NETWORK=langfuse_default
CLICKHOUSE_HOST=clickhouse
CLICKHOUSE_PORT=9000
CLICKHOUSE_DATABASE=default
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=clickhouse
```

Use `compose.langfuse-network.yml` in the `docker compose` command so Grafana
joins that network. If your Langfuse compose project uses a different network
name, set `LANGFUSE_DOCKER_NETWORK` accordingly.

## Tear down

```bash
docker compose --env-file .env -f devtools/grafana/compose.grafana.yml down -v
```

The `-v` flag removes the `grafana-data` volume, wiping dashboards and state.

---

## Grafana MCP Server (Claude Code)

The [grafana/mcp-grafana](https://github.com/grafana/mcp-grafana) server lets
Claude Code query datasources, read and write dashboards, manage alerts, and
navigate your Grafana instance through natural language.

### 1. Create a service account token

After starting the stack, create a token via the API:

```bash
# Create a service account
SA_ID=$(curl -sf -u admin:grafana -X POST http://localhost:3010/api/serviceaccounts \
  -H 'Content-Type: application/json' \
  -d '{"name":"claude-code","role":"Editor"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Generate a token for it
curl -sf -u admin:grafana -X POST http://localhost:3010/api/serviceaccounts/$SA_ID/tokens \
  -H 'Content-Type: application/json' \
  -d '{"name":"claude-code-token"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])"
```

Save the printed token.

### 2. Install mcp-grafana

`mcp-grafana` is distributed as a Go binary and via PyPI. The easiest path if
you already have `uv` installed (which this project requires):

```bash
uv tool install mcp-grafana
```

### 3. Add the MCP server to Claude Code

Replace `<your-token>` with the token from step 1.

**Local scope** (current project only):

```bash
claude mcp add-json "grafana" \
  '{"command":"uvx","args":["mcp-grafana"],"env":{"GRAFANA_URL":"http://localhost:3010","GRAFANA_SERVICE_ACCOUNT_TOKEN":"<your-token>"}}'
```

**User scope** (available across all your projects):

```bash
claude mcp add-json "grafana" --scope user \
  '{"command":"uvx","args":["mcp-grafana"],"env":{"GRAFANA_URL":"http://localhost:3010","GRAFANA_SERVICE_ACCOUNT_TOKEN":"<your-token>"}}'
```

Verify the server is registered:

```bash
claude mcp list
```

### 4. Verify the connection

Start a Claude Code session and ask:

> List my Grafana dashboards.

Claude should respond with the list of the project's dashboards.

---

### Reconfigure with a new token

If you recreate the stack (`down -v`) or rotate the token, update the MCP
server with the new value:

**Local scope** (current project only):

```bash
claude mcp remove grafana
claude mcp add-json "grafana" \
  '{"command":"uvx","args":["mcp-grafana"],"env":{"GRAFANA_URL":"http://localhost:3010","GRAFANA_SERVICE_ACCOUNT_TOKEN":"<new-token>"}}'
```

**User scope** (available across all your projects):

```bash
claude mcp remove grafana
claude mcp add-json "grafana" --scope user \
  '{"command":"uvx","args":["mcp-grafana"],"env":{"GRAFANA_URL":"http://localhost:3010","GRAFANA_SERVICE_ACCOUNT_TOKEN":"<new-token>"}}'
```

### Notes

- The token is tied to the `grafana-data` volume. Running `down -v` destroys
  the service account - repeat steps 1, 3, and the reconfigure step above after
  recreating the stack.
- The **Editor** role is sufficient for most dashboard to work. Use **Admin** if
  you need to manage datasources or users through Claude.
- To restrict Claude to read-only operations, add `"--disable-write"` to the
  `args` array in step 3.

---

## Dashboards

Dashboard JSON files live in `devtools/grafana/dashboards/` and are version controlled. On startup, Grafana provisions them automatically.

Sub-folders under `dashboards/` become Grafana folders.

### Dashboard Inventory

| Dashboard | Primary use | Datasources |
|-----------|-------------|-------------|
| Forge Operations Dashboard | Runtime health, webhooks, queues, retry/dead-letter depth | Prometheus, Redis |
| Forge Ticket Execution Dashboard | Single-ticket trace timeline, step cost, tokens, latency, agent calls | ClickHouse |
| Forge Cost Dashboard | Cost by project, ticket type, model, day, and expensive tickets | ClickHouse |
| Forge Agent Performance Dashboard | Agent invocation rate, duration, latency, prompt size, slow calls | Prometheus, ClickHouse |
| Forge Workflow Funnel Dashboard | Workflow starts/completions/failures, step coverage, rework loops | Prometheus, ClickHouse |
| Forge CI and Review Dashboard | CI fix attempts, approvals, revisions, PR/CI trace signals | Prometheus, ClickHouse |
| Forge Model Usage Dashboard | Calls, cost, tokens, and workflow-step usage by model | ClickHouse |
| Forge Observability Health Dashboard | Prometheus target health and trace metadata coverage | Prometheus, ClickHouse |
| Forge Business Dashboard | Business-level cost and throughput summaries | ClickHouse |
| Forge Engineering Dashboard | Mixed engineering views across runtime, alerts, cost, and latency | ClickHouse, Prometheus, Redis |
| Forge Issue Detail | Detailed per-issue trace and cost drill-down | ClickHouse, Prometheus |

### Workflow

Edited dashboards in the Grafana UI need to be synced back to the repo. Grafana holds live edits in its internal database; however, the local JSON files are the source of truth for version control. On a fresh stack (`down -v && up`), Grafana re-provisions the dashboards from the files.

**Iterate on a dashboard:**

1. Edit the dashboard in the Grafana UI - manually or through the mcp server
2. When happy with the changes, ask the AI to sync it back - e.g. _"save the dashboard back to the source file"_.
3. Claude uses the MCP server to fetch the current dashboard JSON and overwrites the local file.
4. Commit the updated file.

**Create a new dashboard:**

1. Ask Claude to create it - e.g. _"create a dashboard called Trace Volume with a time series panel"_.
2. Claude creates it via the MCP server (appears in the UI immediately).
3. Ask Claude to save it to `devtools/grafana/dashboards/<name>.json`.
4. Commit the file - it will be provisioned automatically on next stack start.
