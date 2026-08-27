# Stage 0 integration baseline

**Status:** Implemented on `prototype/layered-planning-state`

**Baseline date:** 2026-08-27

**Inputs:** `origin/dev`, PR 317 declarative workflow work, and PR 318 layered
planning-state work

## Purpose

Stage 0 establishes one testable starting point for the Option B decoupling work. It
integrates the pending provider and concurrent-review changes with declarative workflow
versioning, node preconditions, generic implementation input, and layered planning state.
It intentionally characterizes existing coupling rather than redesigning station
contracts or effect execution.

## Preserved integration semantics

- Source-control access uses `SourceControlProvider` and the GitHub adapter introduced on
  `dev`; workflow and workspace modules may not import a concrete source-control provider.
- Source-control webhooks retain their normalized queue representation.
- Declarative workflows retain definition identity, revision, digest, resume migration,
  validation, and registered-node preconditions.
- Artifact, work-unit, repository, validation, and publication state remains additive and
  checkpoint compatible with legacy fields.
- Shared post-PR routing retains concurrent CI/review behavior and applies the same
  `ci_evaluator` precondition contract as built-in graphs.
- Task-takeover execution retains generic work resolution and layered state while using
  the asynchronous provider-aware workspace preparation path.
- Review handling uses the provider-neutral authenticated identity while retaining
  thread-settlement behavior across repeated review cycles.

## Automated baseline inventory

Run:

```bash
make architecture-report
```

The report parses every workflow-node module and emits deterministic JSON containing
module line counts, explicit checkpoint-state fields, and integration imports. Its parser
and repository coverage run in the unit-test gate.

Initial combined-tree measurements:

| Measure | Baseline |
|---|---:|
| Workflow node modules | 33 |
| Workflow node lines | 10,173 |
| Explicit state fields read | 85 |
| Integration module families imported by nodes | 5 |
| `OrchestratorWorker` lines | 2,519 |

These are diagnostic baselines, not quality targets. Later stages should reduce broad
state access and worker responsibilities; a larger number of small typed station modules
may legitimately increase module count.

## Enforced architecture boundary

The unit suite rejects imports of legacy GitHub clients or concrete source-control
adapter packages from `forge.workflow` and `forge.workspace`. Provider-neutral contracts
and adapter resolution remain allowed. Jira imports are inventoried but not prohibited in
Stage 0 because removing station-owned effects belongs to Stage 3.

## Characterization coverage

The combined focused gate covers:

- Declarative definition validation, compilation, selection, revision, and migration.
- Layered planning artifacts, invalidation, repositories, and work resolution.
- Source-control contracts, registry behavior, GitHub conformance, and normalized event
  serialization.
- Concurrent CI/review routing and stale-CI attribution behavior.
- Review-thread handling and provider-neutral identity lookup.
- Concrete-provider import boundaries.

The full required PR test gate remains the final regression check for this integration.

## Known coupling retained for later stages

- The worker still performs workflow-stage-specific event interpretation.
- Nodes still accept and return broad workflow-state dictionaries.
- Jira and source-control effects are not governed by a durable effect journal.
- Normalized events are not yet translated into a small versioned workflow-command type.
- Poller/webhook equivalence is not yet tested across repository boundaries.
- Per-workflow serialization is not yet a distributed control-plane guarantee.

These are planned work, not Stage 0 merge blockers.
