# Internals

## Runtime Topology

Forge runs as two process types plus Redis:

- **Gateway**: Single FastAPI/Uvicorn process. Stateless; can be load-balanced.
- **Worker(s)**: One or more `OrchestratorWorker` processes. Each joins the Redis consumer group. **Must run on a host with Podman installed.** Each worker handles up to 20 concurrent tasks (configurable via `QUEUE_MAX_CONCURRENT_TASKS`).
- **Redis**: Single-instance server. No built-in HA; must be provided externally if required.

Gateway and Worker communicate only through Redis and can be deployed on separate hosts. Horizontal Worker scaling has a limitation: per-ticket event serialization uses an in-process `asyncio.Lock`, not a distributed lock (see [Known Limitations](reference.md#known-limitations)).

## State and Event Processing

**Delivery guarantee:** At-least-once. Messages are acknowledged (`XACK`) only after successful processing. The system does not provide exactly-once semantics.

**Checkpointing:** LangGraph workflow state is persisted via `AsyncRedisSaver`, keyed by Jira ticket key (e.g., `AISOS-123`). Checkpoints are written after each graph node completes. When a new event arrives for an existing ticket, the workflow resumes from its last checkpoint.

**Idempotency:** The worker records normalized webhook/poller observations in
the reconciliation ledger before command interpretation. Duplicate, stale,
and conflicting observations do not re-enter the workflow. External writes
still use durable effect identities; provider operations such as Jira comment
posting must remain idempotent across crash recovery.

**Consistency boundary:** Workflow mutations are persisted as stable effect intents before
provider execution. Provider-specific recovery evidence and idempotent ref updates close
the crash window between provider success and checkpoint acknowledgement. Workflow state
and effect state remain separate durable records, joined by the workflow run identity.

## Failure and Recovery

| Component | Failure impact | Recovery |
|-----------|---------------|----------|
| Gateway | Incoming webhooks dropped | Jira/GitHub retry delivery per their own policies |
| Worker | In-flight messages stay in Redis PEL | Restart consumes new messages; PEL requires manual `XCLAIM` |
| Redis | Complete system outage; all state at risk | Configure Redis persistence (RDB/AOF) externally |
| LLM provider | Planning/code generation fails | Retried up to 3 times, then moved to dead-letter queue |
| Container | Non-zero exit captured by orchestrator | Retry mechanism determines re-attempt |

**Retry policy:** Up to 3 attempts with exponential backoff (30s initial, 2x multiplier, capped at 1 hour). Failed messages go to a dead-letter queue for manual investigation.

**Blocked workflows:** The `forge:blocked` label is applied to Jira tickets in error state. Adding `forge:retry` triggers re-entry at the failed step.

**Approval gates:** Workflows pause indefinitely at human review gates. There is no automatic timeout or escalation.

## Security Boundaries

**Webhook authentication:** HMAC-SHA256 validation via `hmac.compare_digest()`. Validation is conditional: it only runs when secrets are configured (`JIRA_WEBHOOK_SECRET`, `GITHUB_WEBHOOK_SECRET`). **Always configure secrets in production.**

**Credential distribution:**

| Credential | Worker | Container |
|------------|--------|-----------|
| Redis | Yes | No |
| Jira API token | Yes | No |
| GitHub App credentials | Yes | No |
| LLM provider (API key or Vertex AI) | Yes | Yes |
| Langfuse | Yes | Yes (when enabled) |
| Git identity | No | Yes |

Containers do not receive Jira, GitHub, or Redis credentials. All external platform operations are performed by the orchestrator after the container exits.

**Container isolation:** Rootless Podman with configurable network mode (`slirp4netns` default), memory limit (4GB), CPU limit (2 cores), and 30-minute timeout. Workspace mounted read-write at `/workspace`; task file read-only at `/task.json`.
