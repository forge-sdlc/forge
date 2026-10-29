# Operations and workflow management

Forge is a durable, event-driven workflow service. This guide explains what
operators run, how a ticket advances, and how to diagnose and safely recover a
workflow without repeating completed agent work or provider writes.

## Runtime services

| Component | Responsibility |
| --- | --- |
| Gateway | Validates Jira and source-control webhooks and places ingress on Redis Streams. It does not make workflow decisions. |
| Worker | Reconciles observations, validates commands, executes the ticket's pinned workflow, runs recovery, and writes the execution timeline. |
| Redis | Stores queue entries, LangGraph checkpoints, observation decisions, effect records, definition/pinning data, and timeline records. |
| Poller | A peer ingress source that observes Jira and source-control changes and forwards them to Forge. It improves recovery; it is not replaced by the gateway. |
| Podman | Runs implementation work in short-lived, rootless containers. The worker remains responsible for their lifecycle. |

The observation ledger, command boundary, definition registry, station runtime,
effect journal, and execution read model are logical control-plane services.
They run with the gateway/worker deployment rather than as additional required
containers.

## Deploying and changing configuration

Run Redis, the gateway, and at least one worker. Run forge-poller where polling
is part of the deployment. After deploying code or changing service
configuration, restart the gateway and workers so routes, registries, and
settings are reloaded.

Preserve Redis during upgrades. It contains recovery and audit records in
addition to queue messages and checkpoints. A Redis flush destroys the evidence
needed to determine whether an external write occurred and can make active
workflows unrecoverable.

`repos.yaml` is loaded once per process. Restart the gateway and worker after
changing it; there is no runtime registry reload. See [configuration](reference/config.md)
for repository, proposal, model, and environment settings.

## How incoming events become workflow work

Webhooks and poller events are at-least-once deliveries. Forge converts each
delivery into an observation, then records a reconciliation decision before
interpreting it:

| Decision | Meaning | Operator action |
| --- | --- | --- |
| Accepted | The provider fact is new and coherent. | No action; the resulting command may advance the workflow. |
| Duplicate | The same provider fact arrived again. | No action. It is intentionally convergent. |
| Stale | A provider revision is older than known evidence. | Investigate provider ordering only if it is unexpected. |
| Conflict | The event cannot safely be ordered or contradicts accepted evidence. | Inspect the timeline and provider record; do not force the workflow forward. |

An accepted observation becomes a command only when it is meaningful at the
saved position of the ticket's pinned workflow. Jira/GitHub facts never set a
workflow position directly.

## Inspecting execution

Set `FORGE_OPERATOR_TOKEN` to enable execution and Org Pulse inspection. Set
`EFFECT_OPERATOR_TOKEN` separately to enable durable-effect inspection and
replay. Requests require the corresponding value as a Bearer token. When the
relevant token is not configured the interface returns `503`; a missing or
invalid token returns `401`.

```text
GET  /api/v1/workflows/{ticket_key}/execution
GET  /api/v1/workflows/{ticket_key}/execution/timeline?cursor=0&limit=50
GET  /api/v1/org-pulse/workflows/{ticket_key}

# Requires EFFECT_OPERATOR_TOKEN
GET  /api/v1/effects/workflow/{run_id}
GET  /api/v1/effects/{idempotency_key}
POST /api/v1/effects/{idempotency_key}/replay
POST /api/v1/effects/{idempotency_key}/reconcile
```

Start with the execution view, then use the timeline to find the accepted
observation, command decision, station attempt, or effect that explains the
current wait or failure. These read endpoints do not advance a workflow or
recompute state from live provider data.

## Effects, retries, and replay

Every workflow-visible Jira, source-control, or repository mutation is a
durable effect. Forge records the intent, leases execution, calls the provider,
and retains attempt and provider evidence under a stable idempotency key.

- **Pending/running effect:** a worker or recovery sweep owns the write. Wait
  for it to settle; a concurrent lease is not a second failure.
- **Retryable failure:** Forge retries with bounded backoff. The workflow fails
  closed while a required effect remains unresolved.
- **Terminal/precondition failure:** correct the underlying provider,
  repository, or configuration problem before recovery.
- **Indeterminate:** the provider may have accepted a write, but Forge cannot
  safely establish the outcome. Inspect the provider before taking action;
  automatic execution and ordinary replay do not repeat this write.
- **Replay:** `POST .../replay` requeues an eligible terminal effect. It repeats
  only that provider mutation; it does not rerun an agent or advance the graph.

Use replay only after confirming the intended write and correcting its cause.
Do not use it as a substitute for inspecting the effect history.

For an indeterminate effect, `POST .../reconcile` requires the same operator token
and an explicit provider-side determination. Its JSON body includes `decision`
(`applied` or `not_applied`), a required `note` describing the evidence, and an
optional `provider_reference`. `applied` records success; `not_applied` returns
the effect to pending execution. The journal retains reconciliation history and
accepts this operation only for an unclaimed indeterminate record. Confirm that
any prior request has settled before choosing `not_applied`.

```json
{
  "decision": "applied",
  "note": "Verified the attachment exists on the issue with the expected content.",
  "provider_reference": "attachment-12345"
}
```

## Upgrading the recovery protocol

Drain and stop all old workers before deploying the ownership-aware worker.
Mixed versions are unsafe: an old worker cannot enforce new claim tokens. Keep
Redis and provider evidence intact throughout the cutover. Gateway delivery may
continue while workers are stopped if Redis has adequate capacity.

1. Snapshot Redis and record pending stream deliveries, retry schedules, running
   effect leases, and active workflow checkpoints.
2. Allow legacy running effect leases to expire before the new worker recovers
   them. Retain completed effect records and their stable idempotency identities.
3. Inspect accepted observations lacking processing records. They are ambiguous;
   do not bulk replay them or mark them complete merely because a checkpoint
   exists. Reconcile individual cases against command, checkpoint, and provider
   evidence.
4. Start the new worker at low concurrency and verify pending/retry recovery,
   effect outcomes, and lease-loss logs using a controlled test ticket.
5. Increase concurrency after recovery converges and outstanding work remains
   within the configured dispatch bound.

For rollback, stop the new workers first and preserve their records. Do not run
an old writer against new active ownership records. Restore only from a reviewed
snapshot/reconciliation procedure that accounts for provider writes made since
that snapshot.

Monitor `forge_queue_active_deliveries`, `forge_queue_buffered_deliveries`,
`forge_queue_reclaimed_deliveries_total`, and `forge_queue_lease_losses_total`
alongside stream pending counts. Dispatch gauges describe admitted local work;
the backlog remains in Redis.

`forge_observation_processing_pending` reports the accepted work still awaiting
completion. `forge_observation_processing_oldest_age_seconds` advances with time
between ledger updates, making stalled pending work visible.

The counter `forge_ticket_stale_writes_rejected_total` identifies rejected stale
checkpoint operations. Inspect ownership-loss logs alongside delivery recovery
and effect history; a rejection protects state and does not itself establish
that the underlying provider operation failed.

See [Delivery recovery and ownership](architecture/recovery.md) for the actual
boundaries. Recovery requires durable Redis data and can repeat model work that
had not yet been checkpointed.

## Managing blocked workflows

When Forge cannot safely continue, it adds `forge:blocked` and posts a Jira
comment with the failure. Common causes include an unresolved required effect,
an invalid station result, an unrecognized command, conflicting provider
evidence, a missing workflow route, or incomplete repository configuration.

1. Read the Jira error and execution timeline.
2. Correct the root cause (for example, repository configuration or provider
   permission), or replay the specific eligible terminal effect.
3. Add `forge:retry` to request a fresh run from the saved failed position.

`forge:retry` does not restart the ticket from intake. It clears the blocked
state after command validation and resumes from the durable checkpoint. It also
resets a depleted CI-fix budget. At `review_response_gate`, it clears contested
review comments and returns the ticket to human review.

See [Jira labels](guide/labels.md) and [PR commands](guide/pr-commands.md) for
the human controls available to each workflow.

## Monitoring and incident response

Use the execution timeline for ticket-specific causality, worker/gateway logs
for process diagnostics, and the existing Prometheus, Langfuse, and Grafana
views for throughput, latency, model use, CI behavior, and system health.

During an incident, preserve Redis, record the ticket/run/effect identities,
and inspect provider evidence before retrying. Restarting a worker is safe when
durable records are intact; blindly re-running agents or deleting state is not
an equivalent recovery procedure.
