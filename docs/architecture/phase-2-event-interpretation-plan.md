# Phase 2 implementation plan: event interpretation outside the worker

**Status:** In progress

**Depends on:** Phase 1 domain contracts

**Goal:** Reduce `OrchestratorWorker` to transport consumption, correlation, instance
locking/resolution, checkpoint invocation, acknowledgement and terminal failure handling.
Provider events are converted into observations and workflow commands by independently
testable adapters.

## Delivery slices

1. **Ingress adapter registry.** Extract source normalization, ticket-type evidence,
   source-control observation conversion, PR correlation evidence and generic source
   dispatch. Preserve compatibility wrappers for existing tests and callers.
2. **Pure resume-command derivation.** Convert Jira label/comment signals and
   source-control review/check/comment signals into versioned `WorkflowCommand` objects.
   Commands do not assign graph nodes.
3. **Exceptional interaction handlers.** Move proposal-review, skip-gate, rebase and
   automated-review interactions behind registered command handlers. Provider feedback is
   emitted through narrow injected ports.
4. **Worker reduction and durable ignored-command evidence.** Make the worker resolve the
   workflow, validate/apply commands and invoke the checkpoint. Record invalid, stale and
   irrelevant commands with reasons.
5. **Conformance and measurement.** Prove adapters run without Redis, LangGraph, Jira or
   GitHub clients; replay duplicate/out-of-order fixtures; compare behavior with the Phase
   0 baseline and record worker-size reduction.

## Compatibility rules

- Existing Redis `QueueMessage` and normalized source-control payloads remain readable.
- Graph topology and checkpoint schemas do not change in this phase.
- Existing worker helper methods remain temporary delegating facades while tests migrate.
- Adapters may depend on Forge domain/provider-neutral contracts, but never provider
  clients, Redis connections, LangGraph or workflow implementations.
- Observations describe external facts. Commands request evaluation; neither may write
  `current_node`.

## Exit criteria

- Adding an ingress source or provider event mapping requires registering an adapter, not
  editing `OrchestratorWorker`.
- Event adapters are deterministic and testable without infrastructure clients.
- Approval, rejection, retry, cancel and synchronize signals become versioned commands.
- Invalid or irrelevant commands have inspectable reasons.
- The worker contains no Jira/GitHub payload-shape interpretation.
- Existing event/resume characterization suites remain behaviorally equivalent.
