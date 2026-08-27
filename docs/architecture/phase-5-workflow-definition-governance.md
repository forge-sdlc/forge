# Workflow-definition governance policy

**Status:** Active

This policy governs every Forge `WorkflowDefinition`, whether it is shipped by Forge or
published by a project administrator. A definition is a release artifact: it is compiled,
validated, reviewed, and published as canonical JSON before it can be selected by a new
workflow instance. LangGraph is an execution target and cannot weaken these rules.

## Definition classes

Forge has two classes of definitions:

| Class | Owner and purpose | Permitted change path |
| --- | --- | --- |
| **Golden path** | Forge owns the feature, bug, and task-takeover processes, including their state profiles, required gates, station contracts, and effect policy. | A Forge code/release change publishes the definition through the same compiler used for custom definitions. Golden paths are the compatibility baseline. |
| **Custom** | A project administrator composes a supported state profile from Forge-registered stations, routers, gates, joins, and extension points. The project owns its operational choice; Forge owns the runtime contracts and safety policy. | Validate and publish through the workflow-definition API/CLI. Custom definitions cannot add Python, expressions, arbitrary imports, or unregistered topology. |

Custom definitions may omit optional golden-path stages, but may not bypass a mandatory
policy, contract, approval boundary, or effect restriction. A custom definition that needs a
new station, router, state profile, or effect capability is an extension proposal, not a
custom YAML change; it requires a Forge-reviewed extension and a new registered catalog
entry first.

## Ownership and review

Every definition has one accountable owner and a named operational contact in its release
metadata. Ownership is not delegated by merely granting project administration permission.

- Forge maintainers own golden-path definitions, the catalog of stations/routers/gates,
  compatibility rules, and the mandatory policy set.
- The project administrator owns a custom definition's intent, project selection label,
  rollout scope, migration decision, and incident response contact.
- The platform/operator team owns publication storage, activation controls, revision
  retention, audit logs, and rollback execution.
- Security/reliability reviewers must approve any change to effect capabilities, external
  writes, approval semantics, joins/concurrency, or recovery behavior.

The required review level is determined before publication:

1. A documentation-only or description change still requires the definition owner and one
   peer reviewer.
2. A compatible topology or routing change requires the owner, a Forge workflow reviewer,
   and an automated validation report.
3. A policy, contract, capability, migration, or breaking change requires the owner, a
   Forge maintainer, and a security/reliability reviewer. The release record must include
   an impact report, migration simulation, rollout/rollback plan, and named approvers.

No author may self-approve a change that they classify as breaking. Emergency publication
is permitted only to restore service or remove an unsafe capability; it must preserve an
immutable revision and receive retrospective review within one business day.

## Mandatory policies and contracts

Publication fails closed unless the definition declares a supported state profile and passes
all of the following checks:

- Every node, router, gate, join, and extension is in the Forge registry for that profile;
  every station contract and version matches the state profile's registered binding, and
  every routed outcome is covered by the reviewed routing contract.
- Every station outcome has an explicit route or terminal handling. Unknown outcomes,
  implicit fall-through, unreachable nodes, unbounded transitions, and unguarded cycles are
  rejected. Cycles must cross an approved human or CI pause boundary and have a bounded
  retry policy.
- Required preconditions are declared and evaluated before a station can request an
  external effect. A missing repository, workspace, pull request, approval, or other
  structural input blocks the station rather than being inferred.
- Required gates remain present for the profile. For example, artifact approval and the
  implementation/review/CI boundaries cannot be replaced with a direct edge to an
  external write. A definition must explicitly declare whether code changes, a pull
  request, CI, and human review are expected when those capabilities are optional.
- Join steps declare `all` or `any` and are rejected without multiple incoming paths.
  Dynamic fan-out declares every target and a maximum cardinality; runtime routing rejects
  undeclared targets or branch counts above that bound.
- The compiler emits a canonical manifest and digest; the review report includes the
  rendered topology, contract versions, policy decisions, effect capabilities, and change
  impact against the prior revision.

These checks apply identically to built-in and custom definitions. A project cannot turn a
mandatory policy off through metadata or an extension point.

## Effect capabilities and extension points

Definitions request named capabilities, not provider clients. The initial finite allowlist is:

- `jira.comment`, `jira.labels`, and `jira.status` for workflow signalling;
- `jira.issue_content`, `jira.issue_lifecycle`, and `jira.issue_structure` for bounded
  issue mutations;
- `jira.project_configuration` for explicitly governed project metadata; and
- `source_control.branch`, `source_control.commit`, `source_control.pull_request`, and
  `source_control.review` for repository and review mutations.

Each capability is scoped to the workflow instance, repository/project identity, station
contract, and effect idempotency key. Read-only observations do not grant a write
capability. A station may request only capabilities declared by its catalog entry and the
definition; the durable effect journal remains the only path to an external mutation.
Custom definitions may use existing capabilities subject to project policy. They may not
introduce provider-specific operations, arbitrary HTTP, credentials, shell execution, or
an effect implementation in YAML. Supported extension points are registered station
contracts, routers, gates, join strategies, state-profile fields, and durable effect
capability descriptors. Every extension documents its input/output schema, failure and
retry behavior, authorization scope, idempotency key, and compatibility class.

## Immutable publication and activation

Publication and activation are separate operations:

1. The author submits a definition with a strictly increasing revision. Forge canonicalizes
   it, validates it, computes its digest, and stores the complete artifact and validation
   report as `published`.
2. Publication is rejected if content changes without a revision increment, if the digest
   already identifies different content, or if mandatory review/evidence is missing.
3. An operator or release automation explicitly activates one published revision for a
   project/definition name, optionally with a canary scope and start/end time. Activation
   affects only new instances unless an approved migration is separately executed.
4. Each instance stores the definition name, revision, digest, and activation context at
   creation. Resume uses that pinned immutable artifact; deleting or replacing the active
   pointer cannot change an in-flight instance.
5. Published revisions are immutable and retained for the maximum checkpoint lifetime plus
   the audit-retention period. Rollback activates an earlier revision; it never edits or
   reuses a revision number.

Activation is blocked when validation, compatibility, migration, or canary evidence is
missing. A removed definition remains readable for pinned instances until they finish,
expire, or are explicitly migrated.

## Compatibility classification

The impact report assigns exactly one class to every revision change:

| Class | Examples | Existing instances | Rollout requirement |
| --- | --- | --- | --- |
| **Patch** | Metadata/description change, or a non-executable canonicalization that preserves digest-relevant behavior. | No migration; pinned instances continue unchanged. | Normal review and validation. |
| **Compatible** | Add an unreachable optional node/branch, add an optional field with a default, or add a backward-compatible station contract. | Continue on their pinned revision; opt-in migration only if a resume map is supplied. | Impact report and canary activation. |
| **Migratable** | Rename/replace a node with equivalent state, reorder work after a safe boundary, or change a contract with a deterministic state conversion. | Remain pinned until an approved migration maps every affected checkpoint. | Dry-run simulation, per-instance eligibility, operator approval, and rollback window. |
| **Breaking** | Remove a reachable node/outcome, alter state meaning, mandatory gate, effect capability, join semantics, or contract incompatibly. | Never silently adopt. Pause or complete on the old revision, or use an explicitly approved migration. | Security/reliability review, migration or drain plan, canary, and explicit activation decision. |

If classification is uncertain, use the more restrictive class. A revision rollback is
breaking for instances that have already observed the newer topology unless a compatibility
analysis proves otherwise.

## Migration and resume mappings

Before activating a migratable or breaking revision, the owner supplies a mapping for every
checkpoint shape that can exist in production. A mapping identifies old revision and node,
new revision and node, state-field conversions/defaults, outstanding gate/effect behavior,
and whether the instance is eligible. The migration simulator must exercise completed,
waiting, retrying, fan-out, join, and failure states and report unmapped or ambiguous cases.

Migration is transactional per instance: acquire the workflow lock, validate the pinned
artifact and mapping, write a migration event and new checkpoint, then release the lock.
An effect that is pending or indeterminate is not replayed merely because a node was
renamed; its original effect identity and result remain authoritative. Ineligible
instances stay on the old revision or are placed in an operator-visible blocked state.
Resume mappings are part of the immutable revision artifact and cannot be supplied after
activation without publishing a new revision.

## Deprecation and breaking changes

Deprecation is announced with a replacement revision, owner, end-of-new-instance date,
checkpoint drain deadline, and migration instructions. During deprecation, new instances
may be blocked or routed to the replacement, but pinned instances continue while the old
artifact is retained. Force-expiring an instance requires an incident/owner decision and an
audit record of its recovery or data-loss implications.

Breaking changes require a migration or an explicit drain. The release record must state
which instances are affected, how approvals and effects are preserved, how a failed
migration is recovered, and when the old revision can be retired. Removing the canonical
artifact, changing its digest, or silently adopting a new revision is never a valid
breaking-change procedure.

## Rollout, rollback, and audit evidence

The operator records a pre-activation snapshot, validation output, rendered manifest,
compatibility classification, migration simulation, approvers, target scope, canary
metrics, and rollback trigger. Canary activation starts with a bounded project or instance
cohort and must observe error rate, blocked/resume rate, station contract failures, effect
retries, and unexpected routes before expansion.

Rollback means activating a previously published immutable revision and stopping further
migration. It does not rewrite checkpoints or cancel durable effects. If instances were
migrated, the rollback record must include a reverse mapping or leave those instances on
the migrated revision while new instances use the prior one. Indeterminate external
effects are reconciled through the effect journal before retry or compensation.

Operational audit evidence is append-only and queryable by definition name, revision,
digest, project, and workflow instance. At minimum retain:

- author, owner, reviewers, approvers, timestamps, source commit, canonical artifact, and
  validation/compiler version;
- publication and activation/deactivation events, target scope, canary observations,
  policy decisions, and rollback trigger;
- instance creation/resume with pinned revision, migration eligibility and mapping,
  migration result, blocked reason, and operator action; and
- station contract decisions, transition/outcome decisions, join results, effect IDs and
  attempts/results, and links to incident or recovery records.

Forge publication and activation require an actor and reason, retain the canonical
artifact and impact report, and record the decision append-only. Organizational release
automation is responsible for attaching the additional review, canary, and incident links
required above to that actor/reason evidence before invoking the governed API.
