# Forge architecture

Architecture reference for Forge's versioned workflow control plane, typed stations, reconciliation,
durable effects, execution inspection, and model-output boundaries.

For workflow details, see the [Feature](../guide/feature-workflow.md), [Bug](../guide/bug-workflow.md), and [Task](../guide/task-workflow.md) guides. For API reference, see the OpenAPI spec at `/docs` when the gateway is running.

| Part | Contents |
|------|----------|
| [System and components](overview.md) | Control-plane structure and component responsibilities |
| [Runtime internals](internals.md) | State authority, reconciliation, stations, effects, and security |
| [Reference](reference.md) | Architectural decisions, known limitations, workflow lifecycles |
| [Workflow governance](phase-5-workflow-definition-governance.md) | Definition ownership, compatibility, activation, and migration |
| [Observation contract](phase-6-observation-contract.md) | Shared webhook/poller identity and ordering contract |
| [Reconciliation contract](phase-6-reconciliation-contract.md) | Convergence and drift-handling behavior |
| [Structured model output](structured-output.md) | Typed model responses and provider fallback behavior |
