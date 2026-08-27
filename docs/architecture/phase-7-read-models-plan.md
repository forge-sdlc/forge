# Phase 7 implementation plan: process and execution read models

**Status:** In progress

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

## Current slice

This PR implements slices 1–3 and the read-side contracts required by slices 4–5. It
uses existing checkpoint and Phase 3 effect records. Legacy checkpoints remain readable;
they explicitly report when their canonical definition or observation history predates
the read model instead of guessing from current Jira state.
