# Phase 3 implementation plan: durable external effects

**Status:** Complete

**Depends on:** Phase 1 effect contracts and Phase 2 event/command boundary

**Goal:** Persist external intent before calling a provider, execute it through narrow
provider adapters, and record a durable result so recovery never requires rerunning an
agent station merely to repeat an external write.

## Delivery slices

1. **Journal and leasing.** Store `EffectCommand` records by stable idempotency key,
   index them by workflow run, atomically claim due work, recover expired leases and
   retain terminal results.
2. **Executor runtime.** Resolve provider-neutral operations through a registry, apply
   bounded exponential retry, and turn exceptions into structured `EffectResult`
   records.
3. **First end-to-end effect.** Route Jira resume acknowledgements through the journal.
   Embed the idempotency key in the provider object so a crash after the provider write
   but before the result write is recoverable without duplication.
4. **Effect migration.** Convert remaining direct Jira and source-control writes by
   operation family, preserving their existing behavior and preconditions.
5. **Operational surface.** Add pending/retry/terminal metrics, administrative replay,
   retention policy and workflow-level effect history to the operator API.

## Correctness rules

- A command is durable before an executor is called.
- One logical write has one stable idempotency key across retries and duplicate events.
- Provider calls either support native idempotency or leave searchable recovery evidence.
- Executors do not advance workflow position; reducers consume successful results.
- A retry resumes the effect, not the station that produced it.
- Terminal and precondition failures remain inspectable and are never silently replayed.

## Completion evidence

- Workflow Jira and source-control writes pass through provider-neutral durable ports;
  architecture tests reject imports or registry access that bypass those ports.
- Repository ref pushes are journalled using the intended commit SHA. Recovery treats an
  already-pushed ref as success and prevents an older pending effect from overwriting a
  newer local commit.
- The production worker binds one Redis-backed effect service and pinned workflow
  identity around every graph invocation. Local station execution uses the same ports
  with an isolated in-memory conformance journal.
- Jira comments, labels, descriptions, fields, attachments, transitions, issue creation,
  links, archival, error notices, source-control branches/files/change requests/comments,
  and repository pushes have idempotent executors and stable identities.
- Required mutations fail closed before the workflow invocation can commit its next
  checkpoint. Attempt history, retry state, replay count, provider references, and
  terminal failures remain durable and inspectable.
- `GET /api/v1/effects/{idempotency_key}`, `GET
  /api/v1/effects/workflow/{run_id}`, and `POST
  /api/v1/effects/{idempotency_key}/replay` provide the operational surface. They are
  disabled unless `EFFECT_OPERATOR_TOKEN` is configured and require its bearer token.
- Prometheus reports effect attempts, results by status, and operator replays. Terminal
  retention is exposed by the journal/service API and does not delete pending work.
- Crash-window tests cover expired leases, duplicate submission, provider-success
  recovery, stale push supersession, retry history, explicit replay, and terminal
  retention.
