# Phase 8 implementation plan: compatibility removal

**Status:** Partially implemented; prerequisite migrations remain

**Goal:** Delete superseded execution paths so Forge has one runtime model rather than
permanent legacy and contract-backed implementations.

## Removal rule

A compatibility path may be deleted only when its replacement is authoritative for all
golden paths, restart/replay characterization passes, persisted state has an explicit
migration policy, and rollback does not require the deleted implementation. Phase 8 is
not permission to remove behavior that an earlier partial phase has not replaced.

## Current removal

The Jira and source-control worker handler facades are deleted. Since Phase 2, both
sources register the same generic adapter-driven handler; the source-specific methods had
no runtime or test callers and represented a second, misleading dispatch API.

An architecture test now enforces generic source registration and validates the
machine-readable removal inventory. Entries cannot be marked removed while their paths
remain, or active while all evidence paths disappear.

## Remaining blockers

- Worker resume interpretation remains authoritative until Phase 2 command handlers and
  durable ignored-command evidence are complete.
- Most provider writes remain inline until Phase 3 effect migration is complete.
- Most workflow nodes still read broad checkpoint state until Phase 4 migration is
  complete.
- Built-in golden paths remain Python topology until Phase 5 publication/governance is
  complete.
- Poller convergence contracts from Phase 6 have not been implemented.
- Durable command/observation timelines from Phase 7 remain incomplete.

The inventory at `docs/architecture/phase-8-removal-inventory.json` is the reviewable
exit checklist. Phase 8 is complete only when every entry is `removed` and the associated
architecture tests pass.
