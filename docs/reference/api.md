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

### Operator execution API

Execution inspection is a read-only API protected by the bearer token configured
as `FORGE_OPERATOR_TOKEN`. Requests without a configured token return `503`; an
invalid or missing bearer token returns `401`. The token is never accepted as a
query parameter.

```http
GET /api/v1/workflows/{ticket_key}/execution
GET /api/v1/workflows/{ticket_key}/execution/timeline?cursor=0&limit=50
```

Execution responses are versioned with `schema_version` (`1.0`). The timeline
uses a deterministic integer cursor and returns `next_cursor` until the end;
clients should treat cursors as opaque offsets and request no more than 200
entries at a time. The response is a projection of durable Forge records and
does not consult current Jira labels.

The compact contract intended for Org Pulse is:

```http
GET /api/v1/org-pulse/workflows/{ticket_key}
```

It returns the execution status, current position, waiting/blocking information,
retry count, observation freshness/conflict state, and migration eligibility.
Org Pulse must preserve `schema_version`, tolerate additive fields, and treat
`null` as “not available” (for example, legacy checkpoints have no migration
decision). This endpoint is read-only and uses the same operator token.

Timeline and terminal effect records are subject to the deployment's retention
policy. Retention must not remove pending or running effects; consumers should
not assume an old timeline event is available forever.

**Operational metrics:** `forge_read_model_latency_seconds` measures API
latency; `forge_execution_waiting_age_seconds`,
`forge_execution_retry_count`, `forge_execution_drift_state`,
`forge_execution_blocked_state`, and `forge_execution_migration_eligibility`
expose waiting age, sampled retry count, drift, blocking codes, and migration
eligibility. The retry, drift, blocked, and migration metrics are gauges for the
most recently sampled execution; they are not event counters and repeated GETs
do not inflate totals. `forge_read_model_latency_seconds` and waiting age are
request/sample histograms by design.

Worker metrics are available separately at `http://localhost:8001/metrics`.

### Durable effect API

Effect inspection and replay are deliberately separate from execution reads.
They require `EFFECT_OPERATOR_TOKEN` as a Bearer token; the routes return `503`
when it is unset and `401` for a missing or invalid token.

```http
GET  /api/v1/effects/workflow/{run_id}
GET  /api/v1/effects/{idempotency_key}
POST /api/v1/effects/{idempotency_key}/replay
```

Replay is an operator recovery action. It requeues one eligible terminal effect
using its existing idempotency identity; it does not rerun an agent or advance a
workflow. Inspect the effect's attempts and provider evidence before replaying.
See [Operations](../operations.md) for effect states and blocked-workflow triage.

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
