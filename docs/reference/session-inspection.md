# Session Inspection

Forge exposes a session and observability inspection surface so users can
understand what is happening in their workflow without needing Redis, Langfuse,
or Grafana credentials.

The session summary and aggregate observability tools are deliberately curated.
They do not expose raw prompts, model messages, generated artifacts, tool inputs,
or full trace metadata. Full trace tools are also available when users need the
underlying Langfuse trace data.

## HTTP API

Use the Forge API when you want a direct integration or a quick command-line
check:

```bash
curl http://localhost:8000/api/v1/sessions/AISOS-123/summary
```

The session endpoint returns workflow progress, current status, PR information,
CI status, artifact presence, and observability links when configured.

The observability endpoints read through the Langfuse API:

| Endpoint | Purpose |
|----------|---------|
| `/api/v1/observability/tickets/{ticket_key}` | Ticket cost, tokens, latency, workflow steps, and recent observation metadata |
| `/api/v1/observability/tickets/{ticket_key}/traces` | Full Langfuse traces for a ticket session |
| `/api/v1/observability/traces/{trace_id}` | One full Langfuse trace by id |
| `/api/v1/observability/model-usage` | Aggregate model calls, cost, tokens, and latency |
| `/api/v1/observability/workflow-funnel` | Workflow-step issue, trace, cost, and latency aggregates |
| `/api/v1/observability/health` | Metadata coverage checks for dashboard-required labels |

See the [API reference](api.md#session-summary) for the full response shape.

## Optional Claude MCP Setup

Forge also ships a read-only stdio MCP server named `forge-session-mcp`. This is
for user assistants such as Claude Desktop or Claude Code. It should be added to
the user's assistant configuration, not to Forge's `mcp-servers.json`.

Do not add `forge-session-mcp` to the checked-in `mcp-servers.json`: that file is
loaded by Forge agents themselves, and the session-inspection server is meant for
external user inspection.

### Claude Code

From the Forge repository:

```bash
uv sync
claude mcp add-json "forge-session" \
  '{"type":"stdio","command":"uv","args":["run","forge-session-mcp"],"cwd":"'"$(pwd)"'"}'
```

Then ask Claude for a session summary by Jira ticket key, for example:

```text
Show me the Forge session summary for AISOS-123.
```

### Manual MCP JSON

If your MCP client accepts JSON configuration, add this server entry and adjust
`cwd` to the local Forge repository path:

```json
{
  "forge-session": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "forge-session-mcp"],
    "cwd": "/path/to/forge"
  }
}
```

The server reads the same `.env` configuration as Forge, so it must be run from a
working Forge checkout with access to the configured Redis/checkpoint backend and
Langfuse API credentials.

## Available MCP Capability

The MCP server provides:

| Capability | Description |
|------------|-------------|
| `get_session_summary` | Tool that returns a safe summary for a Jira ticket key |
| `get_ticket_observability` | Tool that returns safe cost/token/latency aggregates for one ticket |
| `get_session_traces` | Tool that returns Langfuse traces for one Jira ticket session; full trace data by default |
| `get_trace` | Tool that returns one full Langfuse trace by trace id |
| `get_model_usage` | Tool that returns aggregate model calls, cost, tokens, and latency |
| `get_workflow_funnel` | Tool that returns workflow-step issue, trace, cost, and latency aggregates |
| `get_observability_health` | Tool that returns metadata coverage checks for dashboard-required fields |
| `forge://sessions/{ticket_key}` | Resource URI that returns the same summary as JSON |

The MCP responses include `raw_state_exposed: false` or
`raw_trace_data_exposed: false` for curated responses. Full trace responses use
`full_trace_data_exposed: true`.
