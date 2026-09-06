# Forge architecture

Architecture reference for Forge's versioned workflow control plane, typed stations, reconciliation,
durable effects, execution inspection, and model-output boundaries.

For workflow details, see the [Feature](../guide/feature-workflow.md), [Bug](../guide/bug-workflow.md), and [Task](../guide/task-workflow.md) guides. For API reference, see the OpenAPI spec at `/docs` when the gateway is running.

| Part | Contents |
|------|----------|
| [Control-plane architecture](control-plane.md) | Ownership boundaries and safe project/core extension paths |
| [System and components](overview.md) | Control-plane structure and component responsibilities |
| [Runtime internals](internals.md) | State authority, reconciliation, stations, effects, and security |
| [Reference](reference.md) | Architectural decisions, known limitations, workflow lifecycles |
| [Structured model output](structured-output.md) | Typed model responses and provider fallback behavior |
