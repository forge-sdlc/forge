# Phase 4 implementation plan: contract-backed stations

**Status:** In progress

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

## Current slice

This PR starts slices 1–2 with repository task routing. Its station consumes only the
ticket identity and repository-to-task mapping; its output contains no graph node name.
The reducer alone maps that outcome into legacy checkpoint fields and topology, keeping
existing checkpoints and graphs compatible while proving the intended boundary.

The remaining node families are intentionally migrated in reviewable slices: moving
every node at once would combine contract design, provider-effect migration, prompt
behavior and checkpoint compatibility into one unsafe change.
