# Phase 5 implementation plan: explicit process definition

**Status:** In progress

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

## Current slice

This PR implements slices 1–3 on top of the existing strict declarative workflow format.
It introduces no second executable definition: JSON inspection, Mermaid rendering,
LangGraph compilation and revision comparison all consume the same canonical
`WorkflowDefinition` and digest.

The governance and rollout requirements for these definitions are specified in the
[Workflow-definition governance policy](phase-5-workflow-definition-governance.md). The
policy covers golden paths, custom definitions, ownership and review, mandatory contracts,
effect capabilities, immutable publication/activation, compatibility, migration, and
operational evidence.
