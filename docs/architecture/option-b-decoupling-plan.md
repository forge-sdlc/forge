# Option B alignment and decoupling plan

**Status:** Proposed

**Scope:** Forge control plane, workflow definitions, nodes, provider adapters, and the
Forge–poller boundary

**Intent:** Preserve Forge as the authoritative, checkpointed workflow engine while
reducing coupling between event ingestion, process coordination, station logic, and
external side effects.

## Target architecture

Forge implements the centralized workflow-engine model. LangGraph and Forge-owned
workflow definitions decide which transition is valid next. A workflow instance carries
its position, version, gate outcomes, and references to durable artifacts. The sibling
`forge-poller` project supplies reconciliation observations when webhooks are unavailable
or missed; it does not determine workflow position or select the next station.

The intended boundaries are:

1. **Poller and webhook gateway:** produce normalized, replayable observations.
2. **Event adapters:** translate observations into workflow commands without knowing graph
   topology.
3. **Workflow engine:** owns process position, transition validity, policy gates,
   checkpointing, concurrency, and definition versioning.
4. **Stations:** perform bounded domain operations through versioned input/output
   contracts without reading or replacing the complete workflow state.
5. **Effect handlers:** perform Jira, source-control, execution, and notification writes
   through idempotent commands outside station business logic.
6. **Read models:** explain workflow position, waiting reasons, transition history, and
   external-resource status without changing execution state.

This preserves Option B's single executable process definition while preventing the graph
or worker from absorbing provider details and station implementation logic.

## Impact of the pending `dev` merge

The pending `dev` changes do not alter the target architecture or the rationale for this
plan. They implement useful portions of it and therefore change the starting point and
sequencing:

- The provider-neutral source-control contracts and GitHub adapter are the foundation for
  effect handlers and source-control observations. Extend these contracts; do not add a
  second generic provider interface.
- `NormalizedEvent` and its Redis transport are the starting point for `Observation`.
  Evolve or wrap that type with schema version, observed-resource revision, origin
  (`webhook` or `poller`), and stable deduplication identity rather than introducing a
  parallel event envelope.
- The source-control conformance suite is the model for station, effect-handler, and
  poller/webhook conformance suites.
- The test preventing workflow code from importing the concrete GitHub client is an
  initial architecture fitness check. Generalize it to all concrete providers and to
  prohibited dependency directions.
- Shared post-PR graph wiring removes graph duplication and is a useful intermediate
  step. It is not yet the final station boundary because its routers still read the broad
  workflow-state dictionary.
- The concurrent CI/review work makes command validation and deterministic event handling
  more important: CI, review, and merge observations can legitimately arrive in any
  order, while the workflow graph remains authoritative for progression.

Accordingly, Phase 0 and the source-control portions of Phases 1, 2, and 5 are partially
delivered by `dev`. After merging, first reconcile the provider contracts, normalized
event model, workflow state additions, and shared graph code with the declarative-workflow
and layered-state branches. Then baseline the combined tree before further extraction.

This is a semantic integration, not only a Git merge: both lines modify the worker,
workflow base state, and the built-in graphs. Preserve `dev`'s provider-neutral contracts
and normalized ingress while preserving the current branch's workflow revision,
precondition, artifact, and work-unit semantics.

## Architectural rules

- Workflow position and definition version are authoritative inside Forge. Jira labels,
  GitHub state, and poller events are observations or gate inputs, not an alternative
  program counter.
- A station receives a station-specific input and returns a versioned outcome. It cannot
  mutate arbitrary workflow fields.
- Graph routers use normalized outcomes and policy decisions, not provider payloads.
- External writes are expressed as idempotent effect commands with stable keys. A
  transition is not considered operationally complete until its required effects have a
  durable result.
- Provider-specific types stop at adapter boundaries.
- New declarative workflows compose registered station contracts, gates, and routers;
  they cannot import implementation code or bypass mandatory policies.
- Built-in and declarative workflows use the same runtime contracts and migration rules.

## Delivery plan

### Phase 0 — Baseline behavior and dependency map

**Status:** Implemented. See
[Stage 0 integration baseline](stage-0-integration-baseline.md).

Document the state fields read and written by every node, all provider calls made by each
node, graph routes, checkpoint boundaries, and side effects. Add characterization tests
for the feature, bug, and task golden paths, including duplicate events, restarts between
an external write and checkpointing, revision upgrades, and poller/webhook duplicates.

Deliverables:

- A generated node-to-state/effect dependency report in CI.
- End-to-end fixtures for representative Jira, GitHub, and poller observations.
- Architecture fitness checks that reject new imports from provider clients into the
  workflow-domain and station-contract packages.
- Baseline measures for worker size, fields touched per node, duplicate effects, recovery
  time, and workflow migration failures.

Exit criterion: later phases can demonstrate behavioral equivalence and quantify reduced
coupling.

### Phase 1 — Establish versioned domain contracts

Introduce small, Forge-owned contracts independent of LangGraph and providers:

- `Observation`: source, external identity, resource identity, observed revision/time,
  normalized facts, and correlation metadata.
- `WorkflowCommand`: start, resume, approve, reject, retry, cancel, or synchronize an
  existing instance.
- `StationRequest[T]`: workflow/run identity, station invocation identity, scoped inputs,
  artifact references, policy context, and attempt metadata.
- `StationOutcome[T]`: success, blocked, waiting, retryable failure, or terminal failure,
  plus typed outputs and requested effects.
- `EffectCommand` and `EffectResult`: stable idempotency key, expected precondition,
  provider-neutral operation, and durable result.

After the `dev` merge, these contracts must compose with the existing source-control
contracts. `Observation` should be an evolution or provider-independent wrapper of
`NormalizedEvent`; `EffectCommand` should use `SourceControlProvider` operations through
handlers rather than duplicate them.

Create explicit state projections for each station. Retain the existing `BaseState` as a
checkpoint representation initially, but access it through projectors and reducers:

```text
checkpoint state -> station input projector -> station
station outcome   -> validated reducer       -> checkpoint update
```

Exit criterion: a migrated station has no dependency on a complete feature, bug, or task
state dictionary, and malformed outcomes fail before routing or side effects.

### Phase 2 — Split event interpretation out of the worker

Reduce `OrchestratorWorker` to queue consumption, instance locking, workflow resolution,
checkpoint invocation, acknowledgement, and terminal failure handling. Extract:

- Jira observation adapters.
- GitHub observation adapters.
- Poller-origin normalization and deduplication.
- Approval/rejection/retry command derivation.
- PR-to-workflow correlation.
- Workflow-specific command handlers for exceptional interactions.

Adapters return commands and evidence; they do not assign `current_node`. The workflow
decides whether a command is valid at its current position. Invalid or irrelevant commands
are durably recorded with a reason rather than silently changing state.

The `dev` branch already normalizes source-control webhooks before queue transport. Retain
that ingress normalization, then extract the still-central conversion from normalized
events to workflow commands. Raw payload fallback should be treated as a compatibility
path with an explicit removal milestone.

Exit criterion: adding a provider event does not require editing the central worker, and
event adapters can be tested without Redis, LangGraph, Jira, or GitHub clients.

### Phase 3 — Add an idempotent effect journal

Separate transition computation from external mutation. Persist effect intent before
execution and persist its result after execution. Use a stable key derived from workflow
instance, definition revision, transition/invocation identity, effect type, and logical
target. Handlers implement provider-specific precondition checks and safe replay.

Initially journal the highest-risk effects:

1. PR creation and branch push.
2. Jira issue creation and status/label changes.
3. Jira and GitHub comments.
4. Workspace/runtime creation and teardown.
5. CI/review follow-up operations.

Use an outbox worker or an equivalent durable executor. The graph may wait for required
effect results, but station code must not call provider clients directly.

Exit criterion: crashing after an external write but before the next graph checkpoint
does not duplicate that write, and operators can inspect and retry effects independently.

### Phase 4 — Migrate nodes into independently executable stations

Migrate one vertical slice at a time, beginning with a low-side-effect planning station,
then implementation, review, and publication stages. Each registered station provides:

- Contract name and semantic version.
- Input and output schemas.
- Required capabilities and effects.
- Retry and timeout classification.
- Compatibility declarations.
- A local runner and fixtures.
- Contract and conformance tests.

Keep station execution in-process where appropriate; independence is a contract property,
not a requirement to create a service or container for every node. Expensive or untrusted
stations can use an execution driver without changing graph semantics.

Exit criterion: every golden-path station can be invoked by the local harness from a
fixture and returns the same validated outcome used by LangGraph.

### Phase 5 — Make graphs purely coordinative and governed

Update built-in and declarative graphs so nodes are thin station invocations or explicit
policy gates. Route only on typed outcome categories and documented domain fields. Extend
declarative workflow validation to check:

- Station contract and state-schema compatibility.
- Required organizational gates and policies.
- Complete outcome routing.
- Concurrency and join semantics.
- Effect capability requirements.
- Removed/reordered station migration coverage.
- Workflow and station version compatibility.

Use `dev`'s shared post-PR lifecycle as the first migration target: preserve the common
subgraph, replace broad-state routers with typed CI/review outcomes, and register the same
contracts for built-in and declarative workflows.

Pin new workflow instances to a definition revision. Apply backward-compatible station
updates under an explicit compatibility policy. Require an operator-visible migration or
an explicit opt-in policy before an in-flight instance adopts a structurally newer graph.

Exit criterion: the graph is the readable, versioned source of coordination truth, while
station implementation changes do not require graph changes unless their contract or
process role changes.

### Phase 6 — Formalize poller reconciliation semantics

Keep polling in `forge-poller`, but define and test the cross-project contract:

- Polling and webhooks produce the same normalized observation schema.
- Observation identity is stable across both paths when they describe the same external
  revision.
- Duplicates and older observations are harmless.
- A newer authoritative observation may update external facts but cannot skip a workflow
  transition or overwrite workflow position.
- Drift is classified as expected, reconcilable, policy-blocking, or requiring operator
  intervention.
- Poller cursors are not Forge workflow checkpoints; losing a cursor affects load and
  latency, not correctness.

Add cross-repository contract tests that replay captured provider states through both the
webhook and polling paths and assert identical Forge commands.

Exit criterion: loss, duplication, or reordering on either ingress path converges to the
same workflow state without duplicate effects.

### Phase 7 — Add process and execution read models

Build projections from checkpoints, transition decisions, station invocations, effect
results, and observations. Expose:

- Current workflow definition and pinned revision.
- Current position and permitted commands.
- Why the instance is waiting or blocked.
- Last observation and whether external state is stale or conflicting.
- Station attempts, outcomes, effects, and recovery actions.
- Migration eligibility and incompatibilities.

Do not make dashboards infer position from Jira labels or reconstruct the graph from log
messages.

Exit criterion: an operator can answer “why has this not advanced?” and “what will run
next?” from durable records without reading worker logs.

### Phase 8 — Remove compatibility paths

After all golden paths use contracts, reducers, and the effect journal:

- Remove direct provider calls from stations and graph routers.
- Remove legacy broad-state access where projections exist.
- Remove event-to-node routing from the worker.
- Version or migrate legacy checkpoints, with a documented rollback window.
- Turn dependency-report warnings into enforced architecture checks.

Exit criterion: the old paths are deleted, rather than retained as a second execution
model.

## Recommended migration order

Use vertical slices rather than rewriting the whole engine:

1. Integrate `dev` with the declarative-workflow and layered-state changes, resolve the
   overlapping event/state contracts, and establish combined characterization tests.
2. Migrate the shared post-PR CI/review lifecycle, proving normalized observation to
   command translation under concurrent and out-of-order events.
3. Migrate PR creation, proving that the existing source-control adapter can sit behind
   the effect journal with idempotent replay.
4. Migrate PRD generation and approval, proving station contracts and command handling.
5. Migrate task-takeover planning, proving reusable station contracts across workflow
   profiles.
6. Migrate workspace setup and implementation, proving execution-driver isolation.
7. Migrate multi-repository fan-out/join and aggregate completion.
8. Migrate remaining feature and bug stages, followed by legacy removal.

Each slice should run the old and new decision code in shadow comparison where safe, then
switch one project or workflow revision at a time.

## Implications and trade-offs

### Product and governance

- Forge becomes more explicitly responsible for the golden-path process, compatibility
  policy, mandatory gates, and workflow migrations. This requires product/process
  ownership in addition to infrastructure ownership.
- Project-specific composition becomes safer but more constrained. Teams may combine only
  registered compatible stations and cannot bypass organization policy through arbitrary
  graph code.
- Workflow revisions become release artifacts requiring review, rollout notes, and
  migration support.

### Engineering

- There will initially be more types, adapters, reducers, and translation code. The payoff
  is smaller change blast radius and independently testable components.
- A dual-model migration temporarily increases complexity. It must be time-bounded, with
  per-slice removal criteria, or adapters will become permanent duplication.
- Typed contracts expose ambiguous legacy behavior. Some migrations will require explicit
  product decisions rather than mechanical refactoring.
- LangGraph remains replaceable only if Forge owns the contracts, reducers, workflow
  representation, and history schema rather than exposing LangGraph internals as public
  interfaces.

### Runtime and data

- The effect journal adds storage, an executor, retention policy, and operational states
  such as pending or indeterminate. It materially improves replay safety but introduces
  eventual completion between a transition and its external effects.
- Workflow revision pinning increases the number of definitions supported concurrently.
  Retention and maximum-supported-version policies are required.
- Checkpoint migrations become first-class production operations and require backups,
  dry-run reports, rollback plans, and failure-injection tests.
- Strong per-instance serialization must work across workers; an in-process lock is not
  sufficient once the control plane scales horizontally.

### Operations and observability

- Operators gain precise transition and effect history, but must monitor more queues and
  states: observation ingress, workflow commands, station attempts, and effect execution.
- Dead-letter handling must distinguish an invalid observation, invalid command, station
  failure, graph incompatibility, and effect failure.
- The poller remains independently deployable, but schema-version compatibility and
  end-to-end service-level objectives become shared responsibilities across repositories.

### Security

- Central effect handlers improve credential isolation because stations no longer require
  Jira or source-control credentials.
- Station registration and declarative composition become trust boundaries. Schema
  validation, capability allowlists, signed/versioned packages where applicable, and
  policy enforcement must fail closed.

### Performance and cost

- Validation, journaling, and projections add modest latency and storage use.
- In-process stations avoid unnecessary network overhead; separate execution should be
  reserved for isolation, scaling, or runtime needs.
- Better idempotency and checkpoint recovery reduce repeated inference and duplicate
  external operations, partially offsetting the additional control-plane work.

## Program-level completion criteria

The decoupling effort is complete when:

- The central worker contains no workflow-stage-specific event logic.
- Every station declares and passes versioned input/output conformance tests.
- No station directly performs provider mutations.
- Every consequential external effect is journaled and replay-safe.
- Graphs and gates alone determine valid progression, using typed outcomes.
- Workflow instances have explicit definition-version and migration behavior.
- Poller and webhook observations converge under duplicate, missing, and reordered event
  tests.
- A local harness can execute any station without Redis, the worker, or a running graph.
- Operators can inspect position, eligibility, waiting reason, effects, and supported
  recovery from durable read models.
- Legacy broad-state and direct-side-effect paths have been removed.
