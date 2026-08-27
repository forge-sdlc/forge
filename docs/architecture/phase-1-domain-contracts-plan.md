# Phase 1 implementation plan: versioned domain contracts

**Status:** Implemented in PR 324

**Depends on:** Phase 0 baseline and the stacked integration of `dev`, PR 317, and
PR 318

**Goal:** Introduce Forge-owned, versioned runtime contracts and prove them on one
real station without changing graph behavior, checkpoint compatibility, or external
effects.

## Outcome

At the end of Phase 1, Forge will have a provider- and LangGraph-independent contract
layer for observations, workflow commands, station invocations, station outcomes, and
effect intent/results. The existing graphs and `BaseState` remain operational, but one
station will receive only a typed projection of the state and return a validated outcome
which a reducer applies to the checkpoint.

Phase 1 establishes boundaries; it does not yet extract event interpretation from the
worker, execute effects through a journal, or convert every workflow node into a station.

## Design decisions

### Package boundary

Add a new `forge.domain` package. It may depend on Python and the chosen schema-validation
library, but not on LangGraph, Redis, Jira, GitHub, provider adapters, the worker, or
workflow-specific state types.

Proposed layout:

```text
src/forge/domain/
  identity.py       # workflow, invocation, resource, and idempotency identities
  observations.py   # Observation and normalized facts
  commands.py       # WorkflowCommand and command categories
  stations.py       # StationRequest, StationOutcome, status, and typed payload protocol
  effects.py        # EffectCommand, EffectResult, operation and result categories
  schema.py         # schema-version validation and JSON-safe serialization helpers

src/forge/workflow/stations/
  implementation_input.py  # first provider-independent station

src/forge/workflow/projections/
  implementation_input.py  # checkpoint/provider facts -> station request

src/forge/workflow/reducers/
  implementation_input.py  # validated station outcome -> checkpoint update
```

Contract versions are explicit data, not inferred from the installed Forge version.
Version 1 contracts use strict validation, reject unknown status values, and serialize to
JSON-safe primitives. Payload types are scoped to their station rather than accepting
`BaseState` or an arbitrary dictionary.

### Compatibility approach

- Keep `BaseState` as the LangGraph checkpoint schema in Phase 1.
- Keep existing graph node names and routes.
- Wrap the first station with a projector and reducer behind the existing node/function
  entry point.
- Preserve existing checkpoint fields and legacy planning adapters.
- Convert `NormalizedEvent` into an `Observation`; do not remove or fork the existing
  source-control transport format yet.
- Define effect intent contracts, but continue current inline effects until Phase 3.

This permits rollback to the old implementation without migrating persisted checkpoints.

## Delivery stages

### 1.1 — Contract kernel

Implement the five contract families and their stable identities:

- `Observation`: schema version, source (`webhook`, `poller`, or internal), observation
  identity, external resource identity and revision, observed/received time, normalized
  facts, and correlation metadata.
- `WorkflowCommand`: command identity, workflow target, `start`, `resume`, `approve`,
  `reject`, `retry`, `cancel`, or `synchronize`, evidence references, and requested time.
- `StationRequest[T]`: workflow/definition identity, invocation identity, contract name
  and version, attempt, deadline/policy context, artifact references, and typed input.
- `StationOutcome[T]`: `succeeded`, `blocked`, `waiting`, `retryable_failure`, or
  `terminal_failure`, typed output, requested effects, and structured reason/error.
- `EffectCommand` / `EffectResult`: stable idempotency key, provider-neutral operation,
  logical target, expected precondition, payload, and durable result category.

Add round-trip, malformed-input, forward-version rejection, equality, and stable-identity
tests. Add an architecture test preventing `forge.domain` from importing execution or
provider packages.

**Why it matters:** all later extraction work shares one vocabulary and one compatibility
policy. Invalid station results fail at the boundary instead of becoming corrupt graph
state.

### 1.2 — Observation compatibility adapter

Add a lossless adapter from the existing source-control `NormalizedEvent` to
`Observation`. Define stable observation identity from provider event ID plus resource
revision when available, preserve raw payload only as referenced compatibility evidence,
and record whether the source is webhook or poller.

Do not change queue consumers or worker routing in this stage. Add fixtures proving that
serialization is deterministic and repeated conversion produces the same identity.

**Why it matters:** the poller and webhooks can converge on a common Forge-owned envelope
without coupling the domain layer to GitHub or prematurely rewriting ingress.

### 1.3 — Projection and reducer boundary

Create reusable interfaces for:

```text
checkpoint + normalized facts -> StationRequest
StationOutcome + checkpoint   -> validated state update
```

Reducers must allowlist fields, validate the station name and invocation identity, retain
audit metadata, and reject outputs from a different contract version or workflow run.
They return a partial LangGraph update; they cannot mutate the input state.

Add contract tests for missing inputs, stale invocation IDs, malformed outputs, and
attempted writes outside the allowlist.

**Why it matters:** this is the actual coupling break. A station no longer reads or
returns the complete feature, bug, or task state merely because LangGraph stores it.

### 1.4 — First station: implementation-input resolution

Refactor `resolve_implementation_input` into the first contract-backed station because it
already provides a shared domain operation for feature, bug, and task-takeover flows.

The scoped input contains only repository identity, candidate work units, planning
artifact references/content required for selection, completion markers, and normalized
work-item snapshots. The projector performs compatibility reads from legacy checkpoint
fields and obtains external work-item facts through the existing narrow reader. The
station imports neither `JiraIssue`, `JiraClient`, `BaseState`, nor workflow-specific
state. Its typed output contains the selected work unit, ordered context artifacts,
instructions, and optional summary. The reducer alone creates the existing `artifacts`,
`work_units`, `current_work_unit_id`, and `work_resolution` checkpoint updates.

Keep the current `resolve_implementation_input(state, jira)` entry point as a compatibility
facade during Phase 1. Run the existing feature/bug/task tests against both the legacy
behavior fixture and the contract-backed implementation.

**Why it matters:** it proves that one meaningful operation can be run locally from a
fixture, shared by several graphs, and evolved independently of their full state schemas.

### 1.5 — Conformance, rollout, and measurement

Add a station conformance suite covering version negotiation, JSON serialization,
determinism for identical requests, outcome validation, and reducer field ownership.
Expose a small local runner that accepts a serialized `StationRequest` fixture and emits
a serialized `StationOutcome` without starting LangGraph, Redis, or provider clients.

Regenerate the Phase 0 architecture report and record:

- complete-state fields formerly read by implementation-input resolution;
- fields present in its new request and writable by its reducer;
- prohibited dependencies removed;
- checkpoint and golden-path test equivalence.

Initially enable the facade for all flows because it preserves the public call shape. If
behavior differs, retain a temporary compatibility switch for one release and compare
outcomes in tests; do not dual-execute external effects.

**Why it matters:** the phase ends with an independently testable station and measurable
coupling reduction, not only unused model classes.

## Delivery

Phase 1 was delivered as one isolated PR stacked on PR 318, with the internal stages kept
as reviewable commits and package boundaries:

1. **Contract kernel and architecture rule** — Stage 1.1.
2. **Observation adapter** — Stage 1.2.
3. **Projection/reducer framework and implementation-input station** — Stages 1.3–1.4.
4. **Conformance runner, characterization, and measurements** — Stage 1.5.

The PR requires no checkpoint migration and runs feature, bug, and task-takeover
characterization tests.

### Resulting coupling measures

- The compatibility facade decreased from 253 lines to 86 lines.
- The station receives nine explicitly scoped input groups and writes no checkpoint state.
- The reducer owns exactly four checkpoint fields: `artifacts`, `work_units`,
  `current_work_unit_id`, and `work_resolution`.
- The station imports no Jira/GitHub provider, LangGraph type, `BaseState`, worker, or queue.
- The same serialized request runs through the local runner without Redis, LangGraph, Jira,
  or source-control clients.

## Implications

### Benefits

- LangGraph remains authoritative for process position while station code becomes
  portable and narrowly scoped.
- Provider replacement becomes less invasive because provider models stop at projection
  and adapter boundaries.
- Station contracts become versionable, locally runnable, and suitable for declarative
  graph validation in Phase 5.
- Typed outcomes provide the basis for durable effects, retries, and operator read models.

### Costs and risks

- During migration, Forge carries both broad checkpoint state and narrow station models,
  adding adapters and some duplication.
- Contract versioning creates an ongoing compatibility obligation; versions cannot be
  changed casually once checkpoints or queued requests reference them.
- A generic `facts: dict` or payload escape hatch could recreate the current coupling.
  Its contents must therefore be typed per observation/station and architecture-tested.
- Moving Jira reads out of the station makes projection code temporarily more complex.
- Effect commands are declarative only in this phase; inline side effects remain a known
  crash/replay risk until Phase 3.

## Non-goals

- Replacing `BaseState` or migrating existing checkpoints.
- Rewriting `OrchestratorWorker` event dispatch (Phase 2).
- Executing or persisting effect commands (Phase 3).
- Migrating all nodes into stations (Phase 4).
- Changing graph topology, routing, or approval policy.
- Moving polling into Forge or making poller cursor state authoritative.

## Exit criteria

Phase 1 is complete only when:

1. All five contract families are versioned, strict, JSON round-trippable, and free of
   LangGraph/provider dependencies.
2. `NormalizedEvent` has a deterministic, tested conversion to `Observation` without
   breaking the existing queue format.
3. Implementation-input resolution consumes a typed `StationRequest` and produces a
   validated `StationOutcome` without importing complete workflow state or provider
   models.
4. Its reducer can update only its documented checkpoint fields and rejects stale or
   malformed outcomes.
5. The station runs through the local fixture runner without LangGraph, Redis, Jira, or
   GitHub.
6. Existing checkpoints resume and the feature, bug, and task-takeover golden paths remain
   behaviorally equivalent.
7. The architecture report records a smaller dependency and state-access surface for the
   migrated operation.
