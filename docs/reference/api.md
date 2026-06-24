# API Endpoints

Forge exposes a FastAPI server that receives webhooks and serves metrics.

## Base URL

```
http://localhost:8000
```

## Endpoints

### Health Check

```http
GET /api/v1/health
```

Returns HTTP 200 when the API server is running. Does not check worker or Redis connectivity.

**Response:**

```json
{"status": "ok"}
```

---

### Jira Webhook

```http
POST /api/v1/webhooks/jira
```

Receives Jira webhook events. Validates the signature and enqueues the event for async processing by the worker.

**Required headers:**

| Header | Description |
|--------|-------------|
| `X-Hub-Signature` | HMAC-SHA256 of the request body, using `JIRA_WEBHOOK_SECRET` |

**Supported events:**

- `jira:issue_created` — triggers new workflow if `forge:managed` label is present
- `jira:issue_updated` — handles label changes (approvals, retry)
- `jira:issue_commented` — handles Q&A and revision requests

Returns HTTP 200 immediately. Processing is asynchronous.

---

### GitHub Webhook

```http
POST /api/v1/webhooks/github
```

Receives GitHub webhook events. Validates the signature and enqueues for async processing.

**Required headers:**

| Header | Description |
|--------|-------------|
| `X-Hub-Signature-256` | HMAC-SHA256 of the request body, using `GITHUB_WEBHOOK_SECRET` |

**Supported events:**

- `pull_request` — PR opened, closed, synchronized
- `pull_request_review` — human review submitted
- `check_run` — CI check completed
- `issue_comment` — PR comment (for `/forge skip-gate` commands)

Returns HTTP 200 immediately. Processing is asynchronous.

---

### Prometheus Metrics

```http
GET /metrics
```

Exposes Prometheus-format metrics for the API server.

**Key metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `forge_workflows_started_total` | Counter | Workflows started, labeled by type |
| `forge_workflows_completed_total` | Counter | Workflows completed |
| `forge_ci_fix_attempts_total` | Counter | CI fix attempts |
| `forge_agent_duration_seconds` | Histogram | Agent execution time |

Worker metrics are available separately at `http://localhost:8001/metrics`.

---

### Session Summary

```http
GET /api/v1/sessions/{ticket_key}/summary
```

Returns a safe, read-only summary of a Forge workflow session for a Jira ticket.
This endpoint is intended for users who want to inspect session progress without
direct Redis, Langfuse, or Grafana access.

The response is curated and intentionally excludes raw prompts, model messages,
generated artifacts, tool inputs, and full trace metadata.

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `logs_limit` | `0` | Optional number of Redis log entries to include. Allowed range: `0` to `50`. Keep this at `0` for the safest user-facing summary. |

**Example:**

```bash
curl http://localhost:8000/api/v1/sessions/AISOS-123/summary
```

**Response:**

```json
{
  "summary": {
    "ticket_key": "AISOS-123",
    "found": true,
    "current_node": "implement_task",
    "status": "running",
    "is_paused": false,
    "is_blocked": false,
    "retry_count": 0,
    "last_error": null,
    "ticket_type": "Feature",
    "repository": "org/repo",
    "pr_number": 42,
    "pr_url": "https://github.com/org/repo/pull/42",
    "ci_status": "pending",
    "artifacts_present": {
      "prd": true,
      "spec": true,
      "rca": false,
      "plan": true,
      "epics": true,
      "tasks": true,
      "qa_history": false
    },
    "observability_links": {
      "grafana_issue_detail": "http://localhost:3010/d/forge-issue-detail/forge-issue-detail?orgId=1&var-jira_issue=AISOS-123"
    },
    "raw_state_exposed": false
  },
  "notes": [
    "This summary is read-only and excludes raw prompts, model messages, generated artifacts, and tool inputs."
  ]
}
```

Returns `404` if Forge has no persisted session state for the ticket.

---

### Ticket Observability

```http
GET /api/v1/observability/tickets/{ticket_key}
```

Returns safe Langfuse API aggregates for one Jira ticket: total cost,
tokens, latency, workflow-step breakdown, and recent observation metadata.

The endpoint uses Langfuse API reads only and does not expose raw prompts, model
messages, model outputs, tool inputs, or raw trace payloads.

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hours` | `720` | Lookback window, from 1 hour to 90 days |
| `limit` | `50` | Maximum rows for step and observation lists |

### Ticket Traces

```http
GET /api/v1/observability/tickets/{ticket_key}/traces
```

Returns Langfuse traces for one Jira ticket session. By default `full=true`, so
the response includes full trace details from Langfuse, including raw
input/output fields when Langfuse provides them.

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hours` | `720` | Lookback window, from 1 hour to 90 days |
| `limit` | `10` | Maximum number of traces to return |
| `full` | `true` | Hydrate each trace through the Langfuse trace detail API |

### Trace Detail

```http
GET /api/v1/observability/traces/{trace_id}
```

Returns one full Langfuse trace by trace id, including raw input/output fields
when Langfuse provides them.

### Model Usage

```http
GET /api/v1/observability/model-usage
```

Returns aggregate model calls, cost, tokens, and average latency.

### Workflow Funnel

```http
GET /api/v1/observability/workflow-funnel
```

Returns workflow-step issue count, trace count, observation count, cost, and
latency aggregates.

### Observability Health

```http
GET /api/v1/observability/health
```

Returns metadata coverage checks for the fields required by Forge dashboards,
including missing `project_id`, `ticket_type`, `workflow_step`, and `session_id`
counts.

## Webhook Configuration

### Jira

Configure under **Project Settings → Webhooks**:

- **URL:** `https://your-server.com/api/v1/webhooks/jira`
- **Events:** Issue created, Issue updated, Comment created
- **Secret:** Set `JIRA_WEBHOOK_SECRET` in `.env`

### GitHub

Configure under **Repository Settings → Webhooks**:

- **URL:** `https://your-server.com/api/v1/webhooks/github`
- **Content type:** `application/json`
- **Events:** Pull requests, Pull request reviews, Check runs, Issue comments
- **Secret:** Set `GITHUB_WEBHOOK_SECRET` in `.env`
