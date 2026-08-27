# Phase 7 implementation plan: process and execution read models

**Status:** Complete.

**Depends on:** Versioned process definitions, station outcomes and durable effects

**Goal:** Answer operator questions from durable execution records rather than Jira
labels or worker logs. Read models are projections only: they cannot advance a workflow
or execute an effect.

## Delivery slices

1. **Execution projection.** Combine checkpoint position, pinned definition, permitted
   commands, waiting reason, station history, external observation metadata and effects
   into one versioned response.
2. **Pinned-definition visibility.** Retain the canonical declarative definition with
   checkpoints so inspection never substitutes a newer Jira project property for the
   revision an instance actually runs.
3. **Operator API.** Expose the projection by workflow/ticket identity with explicit
   unavailable fields for legacy checkpoints.
4. **Durable decision and observation history.** Persist command decisions and normalized
   observations, including ignored/stale reasons, then include them in the timeline.
5. **Org Pulse and metrics.** Consume the API for dashboards and measure waiting age,
   retries, blocked causes, stale observations and migration incompatibilities.

## Completion evidence

The Phase 7 work items are implemented in the following boundaries:

1. `src/forge/read_models/timeline.py` provides idempotent in-memory and Redis
   timeline stores. `project_execution` rebuilds observations, command decisions,
   transitions, station attempts, effect attempts/results, migrations, and operator
   actions into a deterministic timeline. Coverage is in
   `tests/unit/read_models/test_timeline_store.py` and the read-model tests.
2. `project_execution` exposes the pinned definition, position, permitted commands,
   waits/blocks, stale/conflicting observations, effects, recovery options, and
   evaluated rule explanations. Legacy checkpoints expose unavailable fields rather
   than consulting Jira.
3. `GET /api/v1/workflows/{ticket_key}/execution` and its authenticated timeline
   endpoint are the stable operator surface. Timeline pagination is bounded to 200
   entries and returns a deterministic cursor. Authentication and contract behavior
   are covered by `tests/unit/api/routes/test_executions.py`.
4. The Org Pulse contract is `GET /api/v1/org-pulse/workflows/{ticket_key}` and the
   versioned `OrgPulseExecution` model in `src/forge/integrations/org_pulse.py`.
   Contract and authentication coverage is in
   `tests/unit/integrations/test_org_pulse.py` and
   `tests/unit/api/routes/test_org_pulse.py`.
5. Read-model latency and waiting-age histograms plus bounded-label gauges for
   sampled retry count, drift, blocking, and migration eligibility are defined in
   `src/forge/api/routes/metrics.py`; recording is covered by
   `tests/unit/api/routes/test_metrics.py`. Sampled-state gauges are deliberately
   not counters, so repeated Org Pulse GETs do not inflate event totals. Event
   counters remain owned by their actual decision/transition writers.
6. `rebuild_execution_timeline` and restart-style loader coverage prove deterministic
   reconstruction from durable checkpoint, ledger, timeline, and effect records.

## Operations, retention, and rollback

Read models and operator routes are inspection-only: they do not advance checkpoints,
execute effects, or issue provider mutations. The architecture guard in
`tests/unit/architecture/test_read_model_boundaries.py` prevents mutation calls and
effect-execution imports from returning to those boundaries.

Timeline retention is exposed as the explicit `purge_before` operation on timeline
stores; terminal effect retention remains the explicit
`EffectService.purge_terminal_before` operation. Pending and running effects are not
eligible for terminal retention. Retention is therefore an operator/deployment
operation, not an implicit action during reads, and its deletion is irreversible
without a backup.

The API and Org Pulse payloads carry `schema_version` (`1.0`). Consumers must tolerate
additive fields and treat absent/`null` legacy fields as unavailable. A read-model
rollback deploys the prior application version; it does not rewrite checkpoints or
effects. If a persisted timeline format changes, take a backup and use an explicit
rebuild/migration before re-enabling the new reader.

The full local stack suite, integration suite, focused Ruff checks, and targeted mypy
checks pass. The documentation build remains unverified because the local Zensical file
watcher hit the environment's `Too many open files` (`EMFILE`) limit; this is recorded
as an environment limitation, not evidence that the documentation is invalid.
