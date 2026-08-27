# Option B completion plan

## Outcome

Complete the migration from Forge's current hybrid implementation to an Option B control plane: normalized observations become validated commands, a durable workflow instance selects valid transitions from a pinned process definition, stations run behind typed boundaries, and external mutations execute as durable effects. Polling remains a reconciliation input rather than workflow state.

## Working rules

1. Complete phases bottom-up. A later phase may develop in parallel, but it cannot be declared complete while an earlier contract it depends on remains provisional.
2. Keep one stacked PR per phase. Add completion commits to the existing phase branch rather than opening replacement PRs.
3. Rebase every descendant after changing a lower branch, in stack order, and use force-with-lease when updating remote branches.
4. Introduce Phase 6 between Phases 5 and 7. Rebase the Phase 7 branch onto it, then rebase Phase 8 onto Phase 7.
5. Establish fixture parity or shadow evaluation before each cutover. Never execute both legacy and replacement external effects.
6. A phase is complete only when its new path is authoritative, its superseded path is removed or explicitly time-boxed, and its exit tests pass.

## Common definition of done

Every phase must have:

- Typed public contracts with compatibility rules.
- Unit, contract, end-to-end, duplicate, stale-event, and restart tests appropriate to the phase.
- Durable evidence for decisions and failures; logs alone do not count.
- Architecture checks that prevent reintroducing the coupling removed by the phase.
- Updated operator and architecture documentation.
- A green full CI run on the phase PR and on the rebased stack tip.
- A rollback or migration procedure for any persisted format or authoritative-path change.

## Phase 0 — Characterize and protect current behavior

Status: complete.

Purpose: preserve externally observable behavior while internals are replaced.

Completion audit:

- Confirm representative feature, bug, takeover, post-PR, rejection, retry, and recovery fixtures remain in CI.
- Map each fixture to a later phase's contract or cutover test.
- Add any production behavior discovered during later migration before changing it.

Exit gate: the characterization suite catches changes to routing, provider mutations, restart behavior, and terminal outcomes.

## Phase 1 — Domain contracts

PR: #324. Status: complete.

Purpose: provide platform-owned types for observations, commands, workflow state, station outcomes, and effects so orchestration no longer depends on provider payloads or LangGraph internals.

Completion audit:

- Freeze the initial compatibility policy and document additive versus breaking changes.
- Verify later phases import these contracts rather than defining local equivalents.

Exit gate: all new control-plane boundaries can be expressed with Phase 1 contracts.

## Phase 2 — Event interpretation and authoritative commands

PR: #325. Status: complete.

Purpose: make provider events inputs to pure interpretation, not direct selectors of graph nodes.

Work:

1. Complete normalized adapters for all supported Jira and source-control event families.
2. Move exceptional paths—proposal review, skip, rebase, automated review, and rejection—into pure command derivation or explicit command handlers.
3. Add durable command-decision records for accepted, ignored, invalid, stale, duplicate, and conflicting observations, including reason and source identity.
4. Make the worker perform only: normalize, derive command, validate, persist decision, and dispatch.
5. Run captured payloads through legacy and new interpretation, resolve parity differences, then switch the command path to authoritative.
6. Remove raw-payload routing and event-to-node selection from the worker.

Exit gate: no provider event directly chooses a workflow node; every event produces a durable, explainable command decision.

## Phase 3 — Durable effects

PR: #326. Status: complete.

Purpose: make every external mutation recoverable, idempotent, and observable across crashes.

Work:

1. Inventory remaining direct Jira, GitHub/GitLab, repository, notification, and sandbox mutations.
2. Migrate each mutation family to effect intents with stable identity, preconditions, leases, attempt history, and terminal results.
3. Ensure required effects complete before the corresponding workflow transition is committed.
4. Define retry classification, backoff, supersession, compensation, and operator replay rules.
5. Add crash-window tests for failure before execution, during execution, after provider success, and before acknowledgement.
6. Add metrics, retention, inspection, and controlled replay APIs.
7. Turn the direct-provider-call architecture inventory into an enforced declining baseline.

Exit gate: replay after any crash cannot duplicate a logical external mutation, and stations/routers do not call providers directly.

## Phase 4 — Station boundaries and graph reduction

PR: #327. Status: complete.

Purpose: isolate domain work in independently runnable stations while leaving coordination to the workflow layer.

Work:

1. Finish the generic station runner, typed input projection, outcome validation, and effect emission boundary.
2. Migrate planning and artifact-generation nodes.
3. Migrate implementation and review nodes.
4. Migrate human gates, rejection paths, and persistence nodes.
5. Cover feature, bug, takeover, multi-repository, and post-PR flows.
6. Reduce graph nodes to transition selection, station invocation, joins, and waits; remove embedded station business logic.
7. Add local station fixtures and conformance tests proving stations run without the control plane.

Exit gate: every supported station runs through the same typed boundary locally and centrally; graphs contain coordination only.

## Phase 5 — Versioned process definitions and governance

PR: #328. Status: complete.

Purpose: make the golden path explicit, inspectable, versioned, and enforceable rather than implicit in Python topology.

Work:

1. Publish built-in definitions for every supported golden-path workflow using the same compiler as custom definitions.
2. Encode stations, outcome routing, gates, joins, concurrency, required policies, and allowed effect capabilities.
3. Pin each workflow instance to an immutable definition revision.
4. Validate state/station compatibility, outcome coverage, mandatory policies, and unsafe cycles or joins before publication.
5. Add change-impact and migration simulation for active instances.
6. Define governance for supported extensions, ownership, review, deprecation, and breaking changes.
7. Replace remaining hard-coded topology with compiled definitions.

Exit gate: the supported process can be rendered from a versioned definition, and changing it requires validation and an explicit rollout decision.

## Phase 6 — Reconciliation and poller convergence

PR: #331 (`phase6/reconciliation-contract`). Status: complete.

Purpose: make webhooks and the existing forge-poller equivalent observation sources while workflow state remains authoritative for transition progress.

Work:

1. Publish the normalized Observation contract and shared conformance fixtures for Forge and forge-poller.
2. Define stable identity so polling and webhook delivery of the same provider revision deduplicate.
3. Enforce monotonic provider revisions and harmless handling of duplicate, stale, reordered, and conflicting observations.
4. Classify drift as expected, automatically reconcilable, policy-blocking, or operator-required.
5. Ensure newer external facts may update projections but cannot skip a valid transition or overwrite workflow position.
6. Keep poller cursors as delivery optimization only; they must not become workflow checkpoints.
7. Add cross-repository tests that replay identical provider states through polling and webhook paths and assert identical command decisions.

Exit gate: lost, duplicated, and reordered delivery converges to the same workflow/effect state without duplicate effects.

Evidence: the versioned Observation fixtures in
`tests/contracts/fixtures/observations/` and
`tests/contracts/fixtures/reconciliation/`, adapter and identity tests in
`tests/contracts/` and `tests/unit/integrations/source_control/`, ledger
classification tests in `tests/unit/reconciliation/`, and replay/worker
convergence tests in `tests/contracts/reconciliation/` and
`tests/unit/orchestrator/test_reconciliation_worker.py`. The explicit
no-native-revision limitation is documented in
`docs/architecture/phase-6-observation-contract.md`; such input is retained
for operator review and never used to infer workflow position.

## Phase 7 — Execution read models and operations

PR: #329, rebased onto Phase 6. Status: complete.

Purpose: answer where work is, why it is waiting, and what happened without reconstructing state from Jira labels or logs.

Work:

1. Persist the complete execution timeline: observations, command decisions, transitions, station attempts/outcomes, effect attempts/results, migrations, and operator actions.
2. Build projections for pinned definition revision, current position, permitted commands, waits/blocks, stale/conflicting inputs, effects, and recovery options.
3. Replace heuristic explanations with explanations derived from evaluated workflow rules and false clauses.
4. Add pagination, retention, access control, and stable operator API contracts.
5. Publish Org Pulse integration contracts and operational metrics for latency, retries, drift, blocking, and migration eligibility.
6. Prove projections rebuild deterministically from durable records.

Exit gate: operators can diagnose and recover an execution using persisted records and APIs alone.

Completion evidence: `docs/architecture/phase-7-read-models-plan.md` records the
implementation evidence for all six work items, including durable timeline storage,
deterministic projection rebuilds, authenticated/paginated APIs, Org Pulse's versioned
contract, bounded operational metrics, and the read-only architecture guard. Retention
and rollback procedures are documented there. The full local stack suite, integration
suite, focused Ruff checks, and targeted mypy checks pass. The local Zensical build
remains unverified because its file watcher hit the environment's `EMFILE` open-file
limit.

## Phase 8 — Compatibility removal and final cutover

PR: #330. Status: partial and intentionally last.

Purpose: delete the legacy architecture after every supported path uses the Option B contracts.

Work:

1. Maintain a zero-ambiguity removal inventory with owner, prerequisite, replacement, and proof for every legacy path.
2. Migrate or version existing checkpoints, with dry-run reporting and a defined rollback window.
3. Remove source-specific handler facades, event-to-node worker logic, direct provider calls, broad shared-state access, legacy queues/aliases, and Python-only topology.
4. Remove compatibility adapters after their measured usage reaches zero.
5. Change declining-baseline architecture tests into zero-tolerance rules.
6. Run all golden paths, upgrade/migration scenarios, crash recovery, and reconciliation tests on the final stack.

Exit gate: all supported flows use normalized observations, authoritative commands, pinned definitions, typed stations, and durable effects; the removal inventory is empty.

## Stack execution order

1. Finish #325, then rebase #326, #327, #328, #329, and #330 in order.
2. Finish #326, then rebase all descendants.
3. Finish #327, then rebase all descendants.
4. Finish #328.
5. Phase 6 is implemented by Forge PR #331 and the companion forge-poller PR #10.
6. Rebase `phase7/execution-read-models` from the old Phase 5 base onto Phase 6; change #329's base to the Phase 6 branch.
7. Rebase `phase8-compatibility-removal` onto the rebased Phase 7 branch; retain #330's Phase 7 base.
8. Finish Phase 6, then Phase 7, then Phase 8, rebasing descendants after each phase changes.
9. Merge bottom-up only after each phase's exit gate and CI pass.

## Implementation cadence

For each unfinished phase:

1. Audit the code against the phase inventory and record exact remaining call sites.
2. Implement one vertical slice with its tests and durable evidence.
3. Run focused tests, architecture checks, and the characterization suite.
4. Update the phase plan and removal inventory with measured status.
5. Repeat until the phase exit gate passes.
6. Rebase descendants, resolve contract changes once, and run the stack-tip CI.

This sequence keeps every PR reviewable while ensuring the final result is a single coherent architecture rather than a collection of parallel abstractions.
