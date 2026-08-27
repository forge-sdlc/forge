# Phase 5 implementation plan: explicit process definition

**Status:** Complete

**Depends on:** Versioned contracts and the Phase 4 station boundary

**Goal:** Make the executable process inspectable without reading predicates, Python
functions or LangGraph state. The Forge-owned definition remains authoritative;
LangGraph is one compiler target rather than the process model itself.

## Delivery slices

1. **Runtime-independent manifest.** Compile workflow YAML into a canonical process
   manifest containing node roles, station contracts, gates and labelled transitions.
2. **Visualization.** Render the same validated manifest as Mermaid or JSON for review,
   documentation and Org Pulse integration.
3. **Change impact.** Compare revisions and report added/removed nodes, changed routing,
   contract changes and missing resume mappings that can strand in-flight work.
4. **Golden-path publication.** Publish Forge's supported feature, bug and task-takeover
   definitions as versioned manifests rather than retaining topology only in Python.
5. **Governance and rollout.** Validate mandatory gates/contracts, compatibility policy,
   supported extension points and revision rollout before publication.

## Delivered

PR #328 now ships feature, bug, and task-takeover as checked-in canonical process
artifacts. JSON inspection, Mermaid rendering, LangGraph compilation, revision comparison,
and runtime selection consume those same artifacts and digests; Python graph builders are
no longer the default topology authority.

The compiler validates mandatory gates and policies, registered station contracts,
complete routing outcomes, effect capabilities, joins, bounded fan-out, retry limits,
reachability, and safe cycles. Compiled execution enforces declared effect capabilities,
dynamic targets, concurrency limits, and retry bounds.

Each new instance persists the complete immutable definition identity and resumes against
that pinned artifact. Revision adoption is an explicit migration operation. Change impact
uses patch/compatible/migratable/breaking classifications, and the migration simulator
reports eligibility for each active checkpoint before rollout.

Publication is project-scoped and immutable. Publish, activate, and rollback are separate
CAS-protected, append-only audited decisions; the CLI cannot overwrite or delete active
history. The same behavior is verified against a real Redis server.

The governance and rollout requirements for these definitions are specified in the
[Workflow-definition governance policy](phase-5-workflow-definition-governance.md). The
policy covers golden paths, custom definitions, ownership and review, mandatory contracts,
effect capabilities, immutable publication/activation, compatibility, migration, and
operational evidence.

## Exit evidence

- Built-in artifact round-trip and digest snapshots prove the packaged process is the
  process compiled by the default runtime.
- Governance tests cover mandatory gates/contracts/outcomes, effect authorization,
  fan-out cardinality, retries, immutable publication, CAS activation, and rollback.
- Pinning and migration tests prove active instances cannot silently adopt a changed
  definition and produce deterministic per-instance dry-run reports.
- Workflow, architecture, and status-transition regression suites remain green.
