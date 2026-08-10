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

1. **Contracts before integrations.** Stable domain contracts and conformance suites
   isolate workflows from provider-specific behavior and declare backend capabilities
   explicitly.
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

## Roadmap themes

### 1. Reliable orchestration foundation

This is the prerequisite for every expansion. Provider and runtime abstractions will
multiply failure modes, so Forge must first make node outcomes and side effects explicit.

**Deliverables**

- Versioned, typed artifact envelope for agent handoffs with outcomes such as
  `actionable`, `no_action`, `needs_input`, and `failed`; retain Markdown as a view.
- Fail-closed routing for missing, malformed, or incompatible artifacts.
- Explicit execution result contract; container or pod failure cannot lead to PR/MR
  creation.
- Idempotent side-effect journal for comments, branches, change requests, deployments,
  and teardown operations.
- Stable correlation index across ticket, repository, branch, PR/MR, CI run, and
  environment; remove title parsing as an identity mechanism.
- Terminal-failure notifications, heartbeat/status updates, retry ownership, and
  actionable transcript error summaries.
- Secret redaction and prompt-injection scanning at repository and output boundaries.
- Short-lived credential support, beginning with Vertex AI OIDC and extending the same
  credential-broker pattern to source and deployment providers.
- Contract, replay, failure-injection, and workflow migration tests.

**Current backlog incorporated:**
[artifact contracts #150](https://github.com/forge-sdlc/forge/issues/150),
[execution failure routing #147](https://github.com/forge-sdlc/forge/issues/147),
[terminal retry notification #140](https://github.com/forge-sdlc/forge/issues/140),
[container transcript errors #79](https://github.com/forge-sdlc/forge/issues/79),
[heartbeats #78](https://github.com/forge-sdlc/forge/issues/78),
[secret redaction #77](https://github.com/forge-sdlc/forge/issues/77),
[prompt-injection scanning #76](https://github.com/forge-sdlc/forge/issues/76), and
[short-lived credentials #82](https://github.com/forge-sdlc/forge/issues/82).

**Exit criteria**

- All workflow transitions consume validated typed outcomes.
- Replaying any supported webhook or worker message does not duplicate a side effect.
- Every terminal failure is visible to the user and carries a correlation ID.
- Failure-injection tests prove that unsuccessful execution cannot create a PR/MR.

### 2. Source control provider platform

“Multiple git sources” has two dimensions: a project may span repositories, and each
repository may live on a different provider or provider instance. Forge already supports
the first for GitHub; this theme adds the second.

**Repository and workflow-state model**

Use canonical repository identities rather than `owner/repo` strings. The provider is a
property of each repository reference, never of the workflow as a whole:

```yaml
id: payments-api
provider: github                 # github | gitlab
connection: public-github        # configured Forge connection
namespace: acme/payments
default_branch: main
change_request_mode: fork        # fork | branch
```

A connection holds base/API URLs, webhook verification settings, credential reference,
TLS/CA configuration, and allowed namespaces. This is essential for multiple internal
GitLab deployments and prevents credentials from being embedded in Jira metadata.

Do not extend the current `current_repo`, `current_pr_number`, and `pr_urls` fields with a
single workflow-level provider. That shape cannot safely represent PR #42 in GitHub and
MR !42 in GitLab, provider-specific CI state, or concurrent events from several
repositories. Replace it with a map of independently progressing repository work items:

```yaml
repository_changes:
  payments-api:                 # stable Forge repository ID
    repository:
      provider: github
      connection: public-github
      namespace: acme/payments
    source_revision: main@abc123
    branch: forge/PROJ-123
    change_request:
      native_id: "42"           # opaque string; never globally unique
      url: https://github.com/acme/payments/pull/42
      state: open
    checks:
      state: passed
      runs: []
    review:
      state: approved
    execution:
      state: completed
  deployment-config:
    repository:
      provider: gitlab
      connection: corp-gitlab
      namespace: platform/deployment-config
    source_revision: main@def456
    branch: forge/PROJ-123
    change_request:
      native_id: "42"
      url: https://gitlab.corp/platform/deployment-config/-/merge_requests/42
      state: open
    checks:
      state: running
      runs: []
    review:
      state: pending
    execution:
      state: completed
```

The durable external identity of a change request is the composite
`(connection, repository_id, native_id)`. URLs are presentation data, and native numbers
are opaque provider-local identifiers. A `current_work_item` may exist as a scheduling
cursor, but it must not be the source of truth for event routing or completion.

Workflow-level status is a derived aggregate over `repository_changes`, using explicit
policy such as `all_required_changes_pass_ci`, `all_required_changes_approved`, and
`allow_partial_completion`. Each work item keeps its own execution, push, change-request,
CI, review, retry, and error state. This permits repositories to progress concurrently
and prevents a GitLab event from overwriting the active GitHub state.

Cross-repository ordering must also be explicit. Work items may declare dependencies—for
example, deploy configuration waits for an application image digest—rather than relying
on list position. Outputs passed between repositories use typed artifacts and immutable
revisions, not mutable branch names.

**Provider contract**

- Repository discovery, clone/fetch URL, and default branch.
- Branch/fork creation and push authorization.
- Pull request / merge request create, update, comment, review, merge status, and close.
- CI status normalization, logs/artifacts lookup, and retry/cancel capabilities.
- Webhook verification, normalized events, actor identity, and delivery deduplication.
- Event routing by the composite change-request identity into exactly one repository work
  item, followed by recomputation of aggregate workflow gates.
- Capability discovery so workflows can degrade deliberately when a provider lacks a
  feature.

**Delivery slices**

1. Extract the existing GitHub implementation behind `SourceControlProvider` and prove
   no behavioral regression.
2. Replace GitHub-specific workflow state and language with provider-neutral repository,
   per-repository change request, review, and check-run models; migrate existing
   checkpoints from the single-current-PR shape.
3. Add GitLab.com support for branch-based merge requests, pipelines/jobs, discussions,
   approvals, and system hooks/project webhooks. The first supported GitLab release must
   also support a workflow mixing GitHub and GitLab repositories; single-provider-only
   project support is not an acceptable milestone.
4. Add self-managed GitLab connections: arbitrary base URL, private CA bundle, proxy,
   version/capability probing, group/project tokens, OAuth/service accounts, and multiple
   simultaneous instances.
5. Harden mixed-provider workflows across multiple simultaneous GitLab instances, with
   independent change requests but one aggregate approval and completion view.

**Exit criteria**

- The same provider contract suite passes for GitHub, GitLab.com, and a supported
  self-managed GitLab version range.
- One test feature produces coordinated GitHub PR and GitLab MR changes.
- Interleaved and replayed GitHub/GitLab events update only their addressed repository
  work item and produce the correct aggregate gate state.
- No workflow node imports a concrete source-control client.
- Connection credentials and private CA material never enter agent prompts or logs.

### 3. Pluggable execution and Kubernetes support

Kubernetes support must cover two separate user needs: deploying the Forge control plane
to Kubernetes and running isolated agent jobs on Kubernetes. They should be deliverable
independently.

**Execution driver contract**

- Submit an immutable execution specification: image digest, command, workspace, resource
  limits, deadline, network policy profile, secrets references, and correlation labels.
- Observe status and heartbeats, stream bounded logs, cancel, collect typed results and
  artifacts, and clean up idempotently.
- Drivers: existing local Podman, then Kubernetes Job; future drivers can include remote
  container services without changing workflow nodes.

**Kubernetes agent execution**

- Kubernetes Jobs with per-run ServiceAccounts, security contexts, quotas, deadlines,
  and TTL cleanup.
- Workspace transport via object storage or purpose-built PVCs; do not assume a shared
  host filesystem.
- Default-deny network policies with explicit egress profiles.
- External Secrets / workload identity integration instead of environment-secret copies.
- Log and artifact size limits, cancellation, orphan reconciliation, and namespace-level
  concurrency quotas.
- Compatibility with vanilla Kubernetes and OpenShift restricted security profiles.

**Forge control-plane deployment**

- Versioned OCI images and Helm chart for API, worker, Redis dependency/external Redis,
  Service, Ingress/Route, probes, PodDisruptionBudget, autoscaling, and metrics.
- Database/checkpoint migrations and documented upgrade/rollback policy.
- HA worker semantics, graceful shutdown, queue draining, backups, and disaster recovery.
- Production security guide and reference values for OpenShift.

This theme implements the intent of
[pluggable sandbox drivers #30](https://github.com/forge-sdlc/forge/issues/30).

**Exit criteria**

- Podman and Kubernetes drivers pass one conformance suite.
- A Forge deployment survives worker replacement without losing or duplicating work.
- An OpenShift restricted-profile installation completes an end-to-end workflow.
- Orphaned jobs and workspaces are reconciled after control-plane restart.

### 4. Product prototyping and workflow evolution

Product prototyping is a discovery workflow for Forge users. Starting from an initial
PRD or feature idea, a user can ask Forge to build one or more competing prototypes,
interact with and revise them, compare their behavior, and feed what was learned back
into the PRD and product plan before committing to a production implementation. This is
distinct from prototyping Forge's own workflow graph, which remains a platform-engineering
capability.

**Deliverables**

- A PRD-to-prototype discovery workflow that turns explicit uncertainties and hypotheses
  into one or more time-boxed prototype options with comparable goals and evaluation
  criteria.
- Isolated, disposable prototype workspaces and optional preview environments where users
  can exercise behavior, provide feedback, and request revisions without creating a
  production PR/MR or representing the prototype as production-ready code.
- Side-by-side comparison of alternative prototypes using user feedback, behavior,
  feasibility, architecture implications, risks, cost, and measured results—not only an
  agent preference.
- A governed learning step that proposes concrete PRD and plan updates, records which
  prototype evidence supports each change, and requires user approval before modifying
  the canonical artifacts.
- An explicit transition from discovery to delivery: discard all options, continue
  prototyping, or select an option and generate a coherent implementation plan. Reuse
  validated decisions and evidence, but regenerate production-quality implementation
  rather than silently promoting disposable prototype code.
- Versioned workflow definition and registry with typed inputs, outputs, gates, retry
  policies, permissions, and capability requirements.
- Workflow scaffold CLI and validation/lint command.
- Visual graph rendering plus a step-by-step simulator using fixture events and recorded
  adapter responses.
- `dry-run` mode: agents may generate artifacts, but external writes are captured as an
  inspectable side-effect plan.
- `shadow` mode: run a candidate workflow against copied/sanitized events without writes
  and compare decisions, cost, latency, and artifacts to the active version.
- Project-level pinning, canary rollout, immutable workflow version per in-flight run,
  checkpoint migration rules, and one-click rollback for new runs.
- Evaluation datasets and scorecards for artifact quality, approval revisions, CI
  first-pass rate, completion rate, cost, and time.
- A stable extension API only after two internal workflow prototypes prove the contract.

**Exit criteria**

- Given one PRD with an unresolved product or implementation choice, a user can create,
  revise, and compare at least two working prototype options before selecting either.
- Approved learnings update the PRD and implementation plan with traceable prototype and
  user-feedback evidence; rejected learnings leave the canonical artifacts unchanged.
- Prototype code and environments are clearly marked disposable, isolated from production
  delivery, and cleaned up according to policy.
- A new experimental workflow can be scaffolded and simulated without editing worker
  routing code.
- Dry-run mode performs zero external mutations, verified by adapter contract tests.
- In-flight workflows remain on their original version during a rollout.
- A candidate version can be promoted or rolled back using measured evaluation results.

### 5. External deployment and ephemeral environments

[Issue #28](https://github.com/forge-sdlc/forge/issues/28) should be refined into a
generic lifecycle-hook and deployment-plugin capability. Deployment controllers,
GitOps systems, and infrastructure-automation services integrate through the same
contract; none are dependencies of Forge core.

**Refined scope**

Forge owns the decision and lifecycle record; the plugin owns infrastructure operations.
The first use case is a preview/demo environment built from unmerged change requests.
Conversational ticket intake is a separate upstream integration and is not required for
the deployment MVP. Creation requires explicit approval or command by default; projects
may opt into automatic creation after CI through policy.

**Environment record**

- Stable environment ID, owner, ticket and PR/MR references.
- Requested template, immutable source revisions/image digests, parameters, and policy.
- Provider operation ID, lifecycle state, timestamps, TTL, cost/size classification.
- Non-secret outputs such as URLs; credentials are delivered through a secret broker or
  one-time access mechanism, never Jira/PR comments.
- Teardown reason, status, retries, and audit history.

**Plugin contract**

- `validate(request)`, `provision(request, idempotency_key)`, `status(operation_id)`,
  `outputs(operation_id)`, and `destroy(operation_id, idempotency_key)`.
- Signed/authenticated callbacks plus polling fallback.
- Capability declaration, health check, timeouts, retry classification, and redacted
  errors.
- Hooks initially available after change-request creation, after required CI passes, and
  on close/merge/ticket completion/TTL expiration. Policy selects which hooks are active.

**Safety policy**

- Allowlisted project templates, parameter schemas, quotas, maximum TTL, concurrency and
  budget limits, approved target connections, and optional human deployment approval.
- Unique Forge environment IDs are passed to plugins; plugins remain responsible for
  provider-specific naming and collision handling.
- A durable reconciler performs teardown. Webhook-only teardown is insufficient because
  events can be missed and Forge can be offline at expiry.
- Provisioning failure must not mutate source history or weaken CI gates.
- Deployment success does not imply production release approval.

**Delivery slices**

1. Proposal and threat model; environment state machine and lifecycle hook contract.
2. No-op/reference plugin and conformance suite; dry-run and manual trigger.
3. Generic job-orchestration plugin for invoking allowlisted automation templates.
4. Kubernetes/GitOps plugin for applying approved, parameterized environment templates.
5. Automatic TTL and PR/MR/ticket teardown reconciliation; access-output delivery.
6. Multiple change requests, refresh/redeploy, quotas, cost reporting, and plugin SDK.

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

**Security deliverables**

- CVE remediation workflow: ingest a vulnerability advisory or scanner finding, resolve
  affected repositories and dependency paths, assess exploitability and priority,
  propose the smallest safe upgrade or mitigation, generate regression tests, and retain
  advisory-to-commit evidence.
- Policy-controlled security stages in implementation workflows: dependency, secret,
  static-analysis, and generated-code weakness scans before publication and again through
  repository-owned CI. Normalize findings into typed artifacts with severity,
  confidence, location, remediation, and suppress/accept-risk decisions.
- Fail closed for findings above project policy thresholds; require an auditable human
  exception for accepted risk. Scanner outage or malformed output must not be treated as
  a pass.
- Harden Forge itself: sandbox capability contracts, default-deny execution profiles,
  pre-push validation, structured execution security evidence, credential isolation,
  secret scanning, and prompt-injection defenses.

**Verification-debt deliverables**

- A dedicated test workflow for existing features and code: discover unverified behavior
  from requirements, incidents, change history, coverage/mutation reports, and code risk;
  prioritize verification debt; generate focused tests without requiring a feature
  implementation; and open reviewable PRs/MRs with traceability to the behavior covered.
- Improve test planning in the normal planning and implementation workflows with explicit
  behavior inventories, risk-based test matrices, negative/boundary/concurrency cases,
  and a clear split between deterministic repository validation and model-reviewed
  evidence.
- Measure meaningful verification gains with behavior/risk coverage, mutation score,
  escaped-defect history, and flaky-test impact rather than line coverage alone.

**Current security backlog incorporated:**
[sandbox capability requirements #265](https://github.com/forge-sdlc/forge/issues/265),
[sandbox hardening #266](https://github.com/forge-sdlc/forge/issues/266),
[structured security evidence #264](https://github.com/forge-sdlc/forge/issues/264),
[fail-closed pre-push validation #263](https://github.com/forge-sdlc/forge/issues/263),
[OpenShell boundary spike #262](https://github.com/forge-sdlc/forge/issues/262),
[credential isolation #82](https://github.com/forge-sdlc/forge/issues/82),
[secret scanning #77](https://github.com/forge-sdlc/forge/issues/77), and
[prompt-injection auditing #76](https://github.com/forge-sdlc/forge/issues/76).

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

**Deliverables**

- Concise Jira progress updates and a provider-neutral aggregate view across all
  repositories and environments.
- Review split artifacts on their parent before creating child tickets; revisions update
  stable items rather than deleting and recreating them.
- Concurrent CI observation and human review where policy allows, while merge readiness
  still requires both.
- Better Langfuse span names and end-to-end workflow statistics: duration, revisions,
  model/token cost, first-pass CI, failure class, and environment lifetime.
- Layered prompt efficiency, context budgets, caching, and per-stage model policy.
- An `auto` model option per stage that dynamically routes by task complexity, context,
  required capabilities, latency/cost budget, and observed quality. Policies must support
  allowlists, deterministic fallback, retry/escalation to a stronger model, and a pinned
  model override for reproducibility.
- MLflow integration as an optional experiment/evaluation backend: record workflow and
  stage parameters, model/provider identity, prompt/artifact versions, datasets, metrics,
  costs, latency, and correlation IDs; compare routing and workflow variants without
  making MLflow a runtime dependency or storing secrets/raw sensitive context by default.
- Pre-change-request validation defined by project policy/skills.

**Current backlog incorporated:**
[revision identity #91](https://github.com/forge-sdlc/forge/issues/91),
[parent-first review #84](https://github.com/forge-sdlc/forge/issues/84),
[concurrent CI/review #137](https://github.com/forge-sdlc/forge/issues/137),
[Langfuse labels #138](https://github.com/forge-sdlc/forge/issues/138), and
[prompt efficiency #39](https://github.com/forge-sdlc/forge/issues/39), plus the existing
workflow-status and statistics proposals.

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

## Recommended sequence

The themes overlap, but their enabling order should be explicit.

| Horizon | Primary outcome | Included work |
| --- | --- | --- |
| **Now: Trust the core** | Forge never advances ambiguously and users can diagnose failures | Typed artifacts, execution failure semantics, correlation/indexing, idempotency, terminal notifications, redaction/injection defenses, sandbox hardening, security evidence, status/telemetry |
| **Next: Create extension seams** | Current behavior runs through stable abstractions | Source-control and issue-tracker provider contracts with built-in adapters; execution driver contract with Podman adapter; versioned workflow definitions; lifecycle hook proposal |
| **Then: Add enterprise backends** | Platform teams can centrally operate Forge in heterogeneous environments using an open, self-hostable platform | Mixed GitHub/GitLab workflows from the first GitLab milestone; GitLab.com and self-managed GitLab; Kubernetes agent Jobs; Helm/OpenShift deployment; short-lived credentials/private CA support |
| **Then: Discover, experiment, and deploy** | Teams can learn through working product options, evaluate workflows, and create governed preview environments | PRD-driven competing prototypes and feedback into planning; simulator/dry-run/shadow/canary; security and verification-debt workflows; dynamic model routing and MLflow evaluation; deployment plugin runtime; allowlisted automation-job plugin; TTL reconciler |
| **Later: Broaden the ecosystem** | External contributors can extend Forge without core changes | GitOps and deployment-controller plugins, supported SDKs, additional ticket/source/execution/deployment adapters, workflow template catalog, organization policy and portfolio analytics |

Do not start all integrations simultaneously. A useful vertical-slice order is GitHub
through the new source contract, Podman through the new execution contract, then one
GitLab instance and one Kubernetes Job. Each abstraction should be proven by at least two
implementations before being declared stable.

## Cross-cutting architecture decisions

These decisions should be captured as proposals/ADRs before implementation:

1. **Configuration ownership:** move from GitHub-shaped Jira properties to project
   configuration referencing centrally managed provider connections.
2. **Plugin boundary:** support both Forge-maintained built-in adapters and separately
   deployed external plugins behind the same versioned interfaces and conformance suites.
   Begin with in-process Python interfaces for trusted built-ins; use a versioned
   HTTP/event contract for external or higher-privilege plugins. Do not load arbitrary
   plugin code into the worker.
3. **State durability:** define which state belongs in LangGraph checkpoints versus a
   queryable operational store for correlation, idempotency, and environment lifecycle.
4. **Workflow compatibility:** define immutable workflow versions and checkpoint
   migrations before user-authored graph definitions.
5. **Identity and authorization:** map Jira, GitHub, GitLab, and Forge service identities
   into an auditable actor model with project policy enforcement.
6. **Support matrix:** publish tested GitLab, Kubernetes, OpenShift, Redis, and plugin API
   versions with deprecation policy.

## Measures of success

Track these by project, provider, workflow version, and execution driver:

- Workflow completion and human-escalation rates.
- Incorrect-advance rate after failed/malformed agent or execution output (target: zero).
- Duplicate external side effects under event replay (target: zero).
- Median and p95 lead time by workflow stage; time waiting for humans is separate.
- Artifact revision count and CI first-pass rate.
- Cost per completed change and model tokens by stage.
- PR/MR review turnaround and change failure/reopen rate.
- Execution queue time, success rate, orphan rate, and cleanup latency.
- Preview-environment provision time, success rate, TTL compliance, and leaked resources.
- Provider/driver conformance pass rate and upgrade compatibility.

## Product decisions

1. **Primary audience:** prioritize enterprise platform teams operating Forge as a
   central service. Preserve open-source self-hosting, extensibility, and contribution;
   local execution of Forge is not a primary product mode.
2. **Kubernetes scope:** support both running the Forge control plane and running isolated
   agent sandboxes, as separately shippable tracks.
3. **Mixed source providers:** a Jira project must be able to mix GitHub repositories and
   repositories from multiple GitLab instances from the first supported GitLab milestone.
4. **Product prototyping:** Forge users start from an initial PRD, explore and revise one
   or more working options, compare the results, and feed approved learning into a
   coherent PRD and plan before production implementation.
5. **Preview environments:** require explicit approval or command by default, with
   project-policy opt-in for automatic creation.
6. **First deployment integration:** prove the external deployment contract by invoking
   an allowlisted automation job before adding declarative environment reconciliation.
7. **Extension model:** provide Forge-maintained built-ins and interfaces/conformance
   suites for separately deployed external plugins, with trust-appropriate isolation.
8. **Issue tracking:** abstract Jira behind an issue-tracker interface. Ship Jira as the
   built-in default while allowing external issue-tracker plugins; Jira is not a permanent
   architectural requirement.

## Near-term proposal backlog

Before implementation, create and review focused proposals in this order:

1. Typed agent artifact and node-result contract.
2. Stable correlation identity and idempotent side-effect journal.
3. Source-control provider contract and repository connection model.
4. Execution driver contract and Kubernetes threat model.
5. PRD-driven product-prototyping workflow and versioned workflow simulation/rollout model.
6. Lifecycle hooks, environment state machine, and deployment plugin contract.
7. Enterprise identity, credentials, and policy model spanning all adapters.
8. Security remediation, generated-code scanning, and accepted-risk policy.
9. Verification-debt workflow and risk-based test-planning contract.
10. Dynamic model-routing policy and MLflow evaluation integration.
