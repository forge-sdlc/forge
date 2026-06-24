# Forge Developer Tools

Local development stack for running Forge services on the host with Prometheus scraping both the API and worker, plus Grafana dashboards for Forge observability.

## Usage

```bash
# Start Redis + Prometheus + Grafana (scrapes host-local processes)
docker compose --env-file .env -f devtools/docker-compose.dev.yml up -d

# In separate terminals, start the local services:
uv run uvicorn forge.main:app --reload --port 8000 --host 0.0.0.0
uv run forge worker
```

**With self-hosted Langfuse** — add the network override so Grafana can reach ClickHouse:

```bash
docker compose --env-file .env \
  -f devtools/docker-compose.dev.yml \
  -f devtools/grafana/compose.langfuse-network.yml \
  up -d
```

> `compose.langfuse-network.yml` requires the `langfuse_default` Docker network to already exist (i.e. Langfuse must be running). Without it the entire stack will fail to start. Omit this file if you are not using self-hosted Langfuse — the Prometheus and Redis datasources work without it.

## Endpoints

| Service | URL |
|---------|-----|
| Forge API | http://localhost:8000 |
| API metrics | http://localhost:8000/metrics |
| Worker metrics | http://localhost:8001/metrics |
| Redis | redis://localhost:6380/0 |
| Prometheus | http://localhost:9092 |
| Grafana | http://localhost:3010 |

## How it works

`prometheus.dev.yml` targets `host.docker.internal` which resolves to the host machine from inside the Prometheus container. The `extra_hosts: host.docker.internal:host-gateway` entry in `docker-compose.dev.yml` enables this on Linux/Fedora.

Grafana provisions dashboards from `devtools/grafana/dashboards/` and datasources from `devtools/grafana/provisioning/`. The dashboards expect Forge to emit Langfuse trace tags and metadata for `ticket_key`, `ticket_type`, `project_id`, and `workflow_step`; `.env.example` enables those fields.

For local self-hosted Langfuse, the `compose.langfuse-network.yml` override joins Grafana to the Langfuse compose network so it can reach ClickHouse at `clickhouse:9000`. Set `LANGFUSE_DOCKER_NETWORK` if your Langfuse compose network is not `langfuse_default`.

To reload Prometheus config without restarting:
```bash
curl -X POST http://localhost:9092/-/reload
```

## patch_checkpoint.py

Directly edit a workflow's Redis checkpoint — useful when a workflow gets stuck:

```bash
uv run python devtools/patch_checkpoint.py <ticket-key> <field=value> [field=value ...]

# Examples:
uv run python devtools/patch_checkpoint.py AISOS-376 \
  current_node=ci_evaluator is_paused=false ci_fix_attempts=0

uv run python devtools/patch_checkpoint.py AISOS-376 \
  'ci_skipped_checks=["e2e-openstack"]'
```

Values are parsed as JSON where possible (`true`/`false`/`null`/numbers/lists), otherwise as strings.

## run-wh.sh

Send a Jira webhook payload to a local Forge instance. Substitutes the ticket ID into the payload and, for revision/question payloads, fetches the latest comment from Jira automatically.

```bash
devtools/run-wh.sh <TICKET-ID>

# Examples:
devtools/run-wh.sh AISOS-741    # shows menu of payloads to send
devtools/run-wh.sh --help       # show usage
```

The script:
1. Shows a numbered menu of all payloads in `tests/payloads/`
2. Substitutes `TEST-123` with your ticket ID
3. For revision/question payloads: fetches the latest comment from Jira (via REST API) and injects it into the payload
4. Sends the payload to `http://localhost:8000/api/v1/webhooks/jira`
5. Saves the final payload to a temp file (path printed) for debugging

Requires `JIRA_BASE_URL`, `JIRA_USER_EMAIL`, and `JIRA_API_TOKEN` in `.env` for comment fetching.
