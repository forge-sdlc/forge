# Phase 3 implementation plan: durable external effects

**Status:** In progress

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

## Current slice

This PR implements slices 1–3: an in-memory conformance journal, Redis production
journal, leased background executor, structured retry/results, workflow-run lookup, a
Jira comment executor and migration of resume acknowledgements. Slices 4–5 remain
explicit follow-up work because converting every provider mutation in one change would
make behavioral review and rollback unsafe.
