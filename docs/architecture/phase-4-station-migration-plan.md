# Phase 4 implementation plan: contract-backed stations

**Status:** Complete

**Depends on:** Phase 1 station contracts, Phase 2 commands, and Phase 3 durable effects

**Goal:** Make LangGraph an orchestration adapter rather than the execution API. Each
business operation receives a narrow `StationRequest`, returns a validated
`StationOutcome`, requests external writes as effects, and can run without a graph,
checkpoint store, queue, or provider client.

## Delivery slices

1. **Reusable station boundary.** Standardize workflow/invocation identity projection,
   outcome ownership validation, local registration, and allowlisted reducers.
2. **Pure coordination stations.** Migrate task/repository routing and aggregation first;
   these expose state coupling without mixing in model or provider behavior.
3. **Planning and generation stations.** Migrate triage, PRD, spec, epic/task planning,
   RCA and question-answering operations behind typed inputs and outputs.
4. **Implementation and review stations.** Migrate workspace-scoped implementation,
   local review, CI evaluation/fix, documentation and review-response operations.
5. **Gate and persistence stations.** Convert provider writes into Phase 3 effects and
   leave gates responsible only for policy evaluation and typed waiting outcomes.
6. **Graph reduction and conformance.** Require graph nodes to contain only
   project/invoke/reduce code, run every station through the local runner, and enforce
   dependency rules preventing station imports of LangGraph, checkpoints and providers.

## Delivered boundary

PR #327 now routes the supported operation families through one registered, validated
station runner: routing and aggregation, approvals, triage, artifact generation, agent
operations, implementation input, sandbox execution, and persistence effects. The
workflow layer projects typed requests and reduces typed outcomes; station handlers do
not import LangGraph, queues, checkpoints, Jira, or source-control providers.

Human-review and post-merge persistence use required durable effects, so checkpoint
progress fails closed when publication fails. Agent and sandbox execution no longer
occur directly in graph nodes. Both synchronous pure stations and asynchronous stations
receive the same request, outcome-ownership, contract-version, and effect validation.

## Exit evidence

- Every built-in station is registered in the standalone runner and accepts serialized
  `StationRequest` fixtures without a graph or control plane.
- Architecture tests reject provider/control-plane imports in stations, direct agent or
  sandbox execution in graph nodes, and workflow calls that bypass the registered
  station runner.
- Feature, bug, task-takeover, multi-repository, review, gate, and status-transition
  suites exercise the compatibility reducers and graph paths.
