# Phase 8 implementation plan: compatibility removal

**Status:** Cutover implemented except for observation-to-transition ownership

**Goal:** Delete superseded execution paths so Forge has one runtime model rather than
permanent legacy and contract-backed implementations.

## Removal rule

A compatibility path may be deleted only when its replacement is authoritative for all
golden paths, restart/replay characterization passes, persisted state has an explicit
migration policy, and rollback does not require the deleted implementation. Phase 8 is
not permission to remove behavior that an earlier partial phase has not replaced.

## Completed cutovers

The Jira and source-control worker handler facades are deleted. Since Phase 2, both
sources register the same generic adapter-driven handler; the source-specific methods had
no runtime or test callers and represented a second, misleading dispatch API.

Phase 8 also removes the legacy Redis stream and `github` source alias, implicit
checkpoint pinning, scalar planning fallbacks, the implementation-input facade, and
repository-key fallback migration. Built-in runtime selection is definition-compiled;
the Python graph adapters remain only as local test harnesses. Architecture tests make
these removals zero-tolerance.

Unpinned checkpoints must now be processed by `migrate_unpinned_checkpoint`. Operators
first run it with `apply=False`, retain the original checkpoint as the rollback backup,
and persist the returned `migrated_state` only when `compatible` is true. Applied state
records the target definition and a seven-day rollback deadline by default. Rollback
means restoring that backup before the deadline; normal resume never performs migration
or rollback implicitly.

## Remaining blocker

The worker resume interpreter still applies CI, merge, review-thread, and proposal-review
observations directly. Phase 2 made explicit user commands authoritative but did not
replace those observation transitions. A forced deletion caused 48 characterization
failures, proving this is live behavior rather than removable compatibility code. It
must move behind a provider-neutral transition boundary selected by the pinned workflow
definition before the final inventory entry can be removed.

The inventory at `docs/architecture/phase-8-removal-inventory.json` is the reviewable
exit checklist. Phase 8 is complete only when `remaining` is empty and the associated
architecture tests and golden-path characterization suite pass.
