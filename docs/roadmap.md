# Forge Product Roadmap

**Status:** Draft for discussion  
**Planning horizon:** Outcome-based; dates and release assignments are intentionally TBD  
**Last reviewed:** 2026-08-10

## Product direction

Forge is a governed SDLC orchestrator for turning product intent into traceable artifacts
and validated changes through long-running agent workflows. This roadmap evolves Forge
into an extensible control plane spanning issue tracking, source control, execution, CI,
review, and deployment, including policy-governed temporary environments. These systems
remain the authoritative owners of their respective operations.

The durable product boundary is:

- Forge owns workflow state, policy gates, orchestration, audit history, and recovery.
- Provider adapters own interactions with Jira, GitHub, GitLab, and future systems.
- Execution drivers own where isolated agent work runs.
- Deployment plugins own provisioning and teardown in external platforms.
- Agents propose typed outcomes; the workflow validates them and performs side effects.

This direction preserves Forge's differentiator—long-running, human-governed delivery
workflows—while making Jira, GitHub, and Podman defaults rather than permanent limits.

## Product principles

1. **Provider-neutral core.** Workflow state and logic depend on versioned domain
   interfaces, never a specific external system; every backend declares its capabilities
   and proves the same contract through conformance tests.
2. **Fail closed at trust boundaries.** Invalid artifacts, failed execution, ambiguous
   events, or unavailable policy checks stop progression with evidence and a deliberate
   recovery path.
3. **Humans govern consequential changes.** Publication, accepted risk, and deployment
   require explicit, auditable policy gates; automation is enabled by scoped policy, not
   inferred by an agent.
4. **Traceability survives every backend.** Stable Forge identities correlate artifacts,
   actors, policies, repositories, changes, CI runs, environments, and deployments across
   provider boundaries.
5. **Credentials never enter agent context.** Brokers provide short-lived,
   least-privilege operations outside the model boundary, keeping secrets out of prompts,
   artifacts, workspaces, logs, and comments.
6. **A central platform with an open ecosystem.** Forge is centrally operated for
   consistent cross-project governance while remaining open source, self-hostable,
   extensible, and open to contribution—not repositioned as a local developer tool.
7. **Orchestrate infrastructure; do not become its control plane.** Forge owns deployment
   intent, policy, and lifecycle reconciliation; plugins and target platforms own
   provisioning and infrastructure state.

## Product decisions

1. **Primary audience:** enterprise platform teams operating Forge as a central service;
   open source supports self-hosting, extension, and contribution rather than making
   local execution the primary product mode.
2. **Kubernetes scope:** both the Forge control plane and isolated agent execution,
   delivered as separate tracks.
3. **Mixed source providers:** GitHub and multiple GitLab instances must coexist within
   one project from the first supported GitLab milestone.
4. **Product prototyping:** users compare working options from an initial PRD and feed
   approved learning into the PRD and plan before production implementation.
5. **Preview environments:** explicit approval by default, with project-policy opt-in to
   automatic creation.
6. **First deployment integration:** invoke an allowlisted automation job before adding
   declarative environment reconciliation.
7. **Extension model:** Forge-maintained built-ins and separately deployed external
   plugins share interfaces and conformance suites with trust-appropriate isolation.
8. **Issue tracking:** Jira is the built-in default behind an interface, not a permanent
   architectural requirement.

## Roadmap themes

Each theme uses the same structure: intended outcomes, related tracking, and observable
exit criteria. Technical schemas and implementation contracts belong in the linked issues
and proposals. Tracking status was reviewed on 2026-08-10.

### 1. Deterministic and recoverable orchestration

Forge is moving toward transitions that are validated, replay-safe, traceable, and
recoverable. These capabilities should be delivered incrementally and adopted by each
new provider, runtime, and workflow, enabling expansion without multiplying ambiguous
state or unrecoverable failures.

**Outcomes**

- **Explicit outcomes:** nodes and execution drivers return validated, versioned results
  with unambiguous completion and failure semantics; invalid results fail closed.
- **Replay-safe effects:** an idempotent journal governs comments, branches, PRs/MRs,
  deployments, and teardown so retries and duplicate events cannot repeat mutations.
- **Durable identity:** stable correlation IDs link tickets, repositories, revisions,
  PRs/MRs, CI runs, and environments without relying on titles or URLs for routing.
- **Visible recovery:** heartbeats, status updates, transcript summaries, retry ownership,
  and terminal notifications show what failed, who acts next, and how work can resume.
- **Baseline trust controls:** redact secrets, scan untrusted instructions and outputs,
  and broker short-lived credentials outside agent context, beginning with Vertex AI
  OIDC.
- **Proof under failure:** contract, replay, failure-injection, and migration tests verify
  these guarantees across restarts and workflow upgrades.

**Tracking**

- Open issues: [typed artifact contracts #150](https://github.com/forge-sdlc/forge/issues/150),
  [structured model outputs #252](https://github.com/forge-sdlc/forge/issues/252),
  [heartbeats #78](https://github.com/forge-sdlc/forge/issues/78),
  [container transcript errors #79](https://github.com/forge-sdlc/forge/issues/79), and
  [idempotent terminal retrospectives #261](https://github.com/forge-sdlc/forge/issues/261).
- Active PRs: [heartbeat logging #158](https://github.com/forge-sdlc/forge/pull/158),
  [transcript error surfacing #239](https://github.com/forge-sdlc/forge/pull/239), and
  [terminal retrospectives #271](https://github.com/forge-sdlc/forge/pull/271).
- Merged foundations: [execution failure routing #147](https://github.com/forge-sdlc/forge/issues/147),
  [terminal retry notification PR #155](https://github.com/forge-sdlc/forge/pull/155),
  [per-run trace IDs #80](https://github.com/forge-sdlc/forge/issues/80), and
  [cross-worker ticket serialization PR #212](https://github.com/forge-sdlc/forge/pull/212).
- Tracking gap: the side-effect journal and cross-provider correlation index do not yet
  have dedicated issues.

**Exit criteria**

- Every transition consumes a validated outcome; unsuccessful or unknown execution cannot
  create a PR/MR or cross another consequential gate.
- Replaying supported events or recovering after restart produces no duplicate effect and
  preserves correlation history.
- Every stalled or terminal workflow exposes its failure, owner, evidence, and supported
  recovery action.

### 2. Source control provider platform

“Multiple git sources” has two dimensions: a project may span repositories, and each
repository may live on a different provider or provider instance. Forge already supports
the first for GitHub; this theme adds provider choice without weakening coordinated
multi-repository delivery.

**Outcomes**

- GitHub, GitLab.com, and self-managed GitLab operate behind one provider-neutral source
  control capability, with provider differences exposed deliberately.
- A single workflow can mix GitHub repositories and repositories from multiple GitLab
  instances from the first supported GitLab milestone.
- Repository changes progress independently while Forge presents unified approval, CI,
  and completion status across the workflow.
- Connections, credentials, webhook identity, and provider-specific configuration remain
  centrally governed and outside agent context.

**Sequence**

1. Move current GitHub behavior behind the provider boundary without regression.
2. Introduce GitLab.com together with mixed GitHub/GitLab workflow support.
3. Add self-managed and multiple GitLab instances, then harden compatibility across the
   supported provider matrix.

Issue [#162](https://github.com/forge-sdlc/forge/issues/162) is the canonical technical
plan for repository identity, workflow state, provider contracts, migrations, and event
routing.

**Tracking**

- Open issue: [configurable source providers and mixed-provider workflows #162](https://github.com/forge-sdlc/forge/issues/162).
- Merged foundation: [multi-repository PR lifecycle tracking PR #238](https://github.com/forge-sdlc/forge/pull/238)
  resolved [issue #135](https://github.com/forge-sdlc/forge/issues/135).
- Tracking gaps: the delivery slices in #162 should be split into implementation issues
  as work is scheduled.

**Exit criteria**

- One workflow produces coordinated changes across GitHub and GitLab.com, and across the
  supported self-managed GitLab matrix.
- Mixed-provider events and failures remain isolated to the correct repository change
  while aggregate workflow gates stay correct.
- Provider conformance tests show equivalent core behavior, and credentials or private
  connection material never enter agent prompts or logs.

### 3. Pluggable execution and Kubernetes support

Kubernetes support has two independent tracks: running isolated agent work and operating
the Forge control plane. Both build on provider-neutral execution semantics so workflow
logic does not depend on Podman, Kubernetes, or a future runtime.

**Outcomes**

- Podman and Kubernetes execute the same bounded work specification and expose consistent
  lifecycle, logs, artifacts, cancellation, and cleanup behavior.
- Kubernetes agent jobs use workload identity, least privilege, resource limits,
  controlled egress, and durable workspace/artifact transport.
- Forge ships as an operable Kubernetes/OpenShift service with supported installation,
  upgrades, rollback, scaling, recovery, and production security guidance.
- Runtime failures, restarts, and orphaned resources converge without losing or
  duplicating workflow work.

**Sequence**

1. Establish the execution-driver boundary and retain Podman behavior.
2. Add conformant Kubernetes agent execution and sandbox hardening.
3. Deliver and harden the Kubernetes/OpenShift control-plane distribution.

**Tracking**

- Open issue and implementation PR: [pluggable sandbox drivers #30](https://github.com/forge-sdlc/forge/issues/30)
  and [Kubernetes driver PR #243](https://github.com/forge-sdlc/forge/pull/243).
- Security and runtime design: [OpenShell boundary spike #262](https://github.com/forge-sdlc/forge/issues/262),
  [driver capabilities #265](https://github.com/forge-sdlc/forge/issues/265),
  [sandbox hardening #266](https://github.com/forge-sdlc/forge/issues/266), and
  [structured execution security evidence #264](https://github.com/forge-sdlc/forge/issues/264).
- Tracking gap: production OCI images, Helm/OpenShift deployment, HA operations,
  upgrades, backup, and disaster recovery do not yet have dedicated issues.

**Exit criteria**

- Podman and Kubernetes drivers pass one conformance suite.
- A Forge deployment survives worker replacement without losing or duplicating work.
- An OpenShift restricted-profile installation completes an end-to-end workflow.
- Orphaned jobs and workspaces are reconciled after control-plane restart.

### 4. Product prototyping and workflow evolution

This theme contains two related capabilities: product discovery for Forge users and safe
evolution of Forge workflows by platform engineers. Both rely on isolated experiments,
measured comparison, and an explicit decision before promotion.

**Outcomes**

- Users can turn an uncertain PRD or feature idea into multiple time-boxed prototypes,
  revise and compare them, and approve evidence-backed updates to the PRD and plan.
- Prototype code and environments remain disposable and clearly separate from production
  delivery; validated decisions may carry forward, but production code is regenerated.
- Platform engineers can define, validate, simulate, and evaluate versioned workflows
  without editing orchestrator routing code or mutating live projects.
- Dry-run, shadow, pinning, canary, migration, and rollback controls support measured
  workflow evolution without changing in-flight runs unexpectedly.
- Stable extension APIs follow proven internal implementations rather than speculative
  abstraction.

**Tracking**

- Merged foundation: [PRD approval workflow issue #33](https://github.com/forge-sdlc/forge/issues/33)
  and [implementation PR #83](https://github.com/forge-sdlc/forge/pull/83).
- Open feedback work: [artifact Q&A #163](https://github.com/forge-sdlc/forge/issues/163),
  [decompose draft review #218](https://github.com/forge-sdlc/forge/issues/218), and
  [draft review PR #242](https://github.com/forge-sdlc/forge/pull/242).
- Related developer tooling: [local skill testing #296](https://github.com/forge-sdlc/forge/issues/296).
- Tracking gaps: competing PRD-driven product prototypes, prototype comparison and
  learning capture, disposable preview workspaces, workflow simulation, dry-run/shadow
  execution, version pinning, canary rollout, and rollback need dedicated issues.

**Exit criteria**

- A user can compare at least two working options for one unresolved PRD decision and
  apply only approved learnings to the canonical artifacts.
- Prototype resources are isolated, visibly disposable, and cleaned up by policy.
- A candidate workflow can be simulated, evaluated, promoted, and rolled back without
  external dry-run mutations or unintended changes to in-flight runs.

### 5. External deployment and ephemeral environments

Forge should govern deployment intent and environment lifecycle while external systems
remain responsible for infrastructure operations and state. The first use case is a
preview environment for unmerged changes, created only after explicit approval by
default.

**Outcomes**

- A provider-neutral lifecycle and plugin boundary supports deployment controllers,
  GitOps systems, and automation services without making them Forge dependencies.
- Environment records connect immutable inputs, policy, ownership, status, outputs, TTL,
  teardown, and audit history without exposing credentials.
- Allowlisted templates, quotas, budgets, approvals, and target connections constrain
  what can be provisioned and where.
- Idempotent reconciliation converges provisioning and teardown across duplicate events,
  missed callbacks, expiry, and Forge restarts.

**Sequence**

1. Define the lifecycle, threat model, and reference plugin.
2. Prove the contract by invoking an allowlisted automation job.
3. Add declarative/GitOps integrations, automatic teardown, quotas, cost reporting, and
   an external plugin SDK.

**Tracking**

- Open proposal: [staging/demo environments #28](https://github.com/forge-sdlc/forge/issues/28).
- Tracking gaps: lifecycle hooks, environment state, plugin conformance, allowlisted
  automation jobs, approval policy, and TTL reconciliation need dedicated issues after
  #28 is refined.

**Exit criteria**

- Killing Forge during provision or teardown converges to the correct state after restart.
- Duplicate hooks cannot create duplicate environments.
- Expired environments are detected and destroyed within the defined SLO.
- No infrastructure or access credentials are exposed to agents or ticket/change-request
  comments.

### 6. Security remediation and verification debt

Security and testing are workflow outcomes, not only implementation-stage tools. Forge
should support both urgent vulnerability remediation and systematic improvement of
existing code whose behavior is insufficiently verified.

**Outcomes**

- A CVE workflow traces advisories or findings to affected code, assesses risk, proposes
  minimal remediation, adds regression tests, and preserves advisory-to-change evidence.
- Policy-controlled security stages scan dependencies, secrets, source, and generated
  changes; required checks fail closed and accepted risk requires an auditable exception.
- Forge hardening covers sandbox boundaries, pre-push validation, credential isolation,
  security evidence, secret scanning, and prompt-injection defenses.
- A verification-debt workflow discovers risky unverified behavior in existing code,
  prioritizes it, and opens focused test-only changes with behavior-level traceability.
- Planning uses risk-based behavior matrices and measures verification gains beyond line
  coverage, including mutation effectiveness and flaky-test impact.

**Tracking**

- Open security issues: [sandbox capability requirements #265](https://github.com/forge-sdlc/forge/issues/265),
  [sandbox hardening #266](https://github.com/forge-sdlc/forge/issues/266),
  [structured security evidence #264](https://github.com/forge-sdlc/forge/issues/264),
  [fail-closed pre-push validation #263](https://github.com/forge-sdlc/forge/issues/263),
  [OpenShell boundary spike #262](https://github.com/forge-sdlc/forge/issues/262),
  [credential isolation #82](https://github.com/forge-sdlc/forge/issues/82),
  [secret scanning #77](https://github.com/forge-sdlc/forge/issues/77), and
  [prompt-injection auditing #76](https://github.com/forge-sdlc/forge/issues/76).
- Active security PRs: [pre-push validation #272](https://github.com/forge-sdlc/forge/pull/272)
  and [prompt-injection auditing #287](https://github.com/forge-sdlc/forge/pull/287).
- Merged Forge hardening: [security batch PR #231](https://github.com/forge-sdlc/forge/pull/231)
  resolved issues [#219–#226](https://github.com/forge-sdlc/forge/pull/231),
  covering CORS, authorization, command/path injection, endpoint exposure, error leakage,
  and skill-source validation.
- Verification-debt proposals: [test generation node #256](https://github.com/forge-sdlc/forge/issues/256)
  and [test coverage workflow #257](https://github.com/forge-sdlc/forge/issues/257)
  were closed without implementation; [deterministic pre-PR validation #174](https://github.com/forge-sdlc/forge/issues/174)
  remains open.
- Tracking gaps: CVE intake/remediation, normalized security stages and findings,
  accepted-risk policy, risk-based test planning, mutation/behavior coverage, and flaky
  test accounting need dedicated issues.

**Exit criteria**

- A CVE advisory can produce a policy-compliant remediation PR/MR with linked scan and
  regression-test evidence.
- Generated changes cannot be published when a required security stage fails, is
  unavailable, or returns invalid output.
- A verification-debt run can add tests to existing code and report the behaviors and
  risks newly covered without changing production behavior.
- Forge execution security controls and accepted exceptions are queryable by correlation
  ID.

### 7. Human experience, quality, and economics

Platform breadth is only valuable if users can understand and govern it.

**Outcomes**

- Users see concise progress, ownership, next actions, and aggregate status across every
  repository and environment associated with a workflow.
- Artifact review preserves stable identity and happens before irreversible decomposition
  or publication; CI and human review may proceed concurrently without weakening gates.
- End-to-end telemetry reports outcome, latency, revisions, failures, quality, and cost by
  workflow stage and configuration.
- Per-stage model policy supports a pinned model or auditable `auto` routing with budgets,
  capability constraints, deterministic fallback, and escalation.
- Optional MLflow export compares workflow and routing variants using shared correlation
  IDs without becoming a runtime dependency or exporting sensitive context by default.
- Prompt efficiency, context budgets, caching, and repository-owned pre-PR validation
  reduce cost and avoidable feedback cycles without lowering quality.

**Tracking**

- Open issues and PRs: [revision identity #91](https://github.com/forge-sdlc/forge/issues/91),
  [concurrent CI/review #137](https://github.com/forge-sdlc/forge/issues/137) with
  [PR #143](https://github.com/forge-sdlc/forge/pull/143), and
  [prompt efficiency #39](https://github.com/forge-sdlc/forge/issues/39).
- Merged foundations: [parent-first review #84](https://github.com/forge-sdlc/forge/issues/84),
  [Langfuse labels #138](https://github.com/forge-sdlc/forge/issues/138), and
  [provider-neutral per-stage model policy issue #175](https://github.com/forge-sdlc/forge/issues/175)
  with [PR #251](https://github.com/forge-sdlc/forge/pull/251).
- Tracking gaps: automatic model routing and escalation, routing evaluation baselines,
  MLflow integration, aggregate provider-neutral status, and end-to-end workflow economics
  need dedicated issues.

**Exit criteria**

- Users can identify current stage, owner, next action, and every related PR/MR/environment
  from the parent ticket.
- Every major stage reports latency, cost, revision count, and outcome.
- Baseline and post-change evaluation show cost improvements without lower completion or
  quality scores.
- Automatic routing meets configured quality and latency floors while reducing cost
  against pinned-model baselines, with every routing decision auditable.
- The same evaluation run can be inspected in Forge telemetry and, when configured,
  MLflow using a shared correlation ID.

## Measures of success

Track these by project, provider, workflow version, and execution driver:

- **Reliability:** incorrect advances and duplicate external effects (target: zero), plus
  visible and recoverable terminal failures.
- **Delivery:** workflow completion, human escalation, stage lead time excluding human
  wait, CI first-pass rate, review turnaround, and change failure or reopen rate.
- **Quality and economics:** artifact revisions, quality outcomes, model tokens and cost
  per completed change, and routing performance against pinned-model baselines.
- **Platform operations:** provider/driver conformance, execution queue and success rates,
  orphan cleanup, environment provisioning, TTL compliance, and leaked resources.
