# Reference

## Key Architectural Decisions

### Redis Streams for Event Bus

Use Redis Streams with consumer groups instead of a dedicated message broker (RabbitMQ, Kafka). Redis already serves as the checkpoint store, so reusing it for event queuing eliminates an infrastructure dependency. The tradeoff: no built-in dead-letter queues or cross-datacenter replication.

### LangGraph for Workflow Orchestration

Use LangGraph `StateGraph` with `AsyncRedisSaver` checkpointing instead of Temporal or Airflow. LangGraph provides native LLM-driven decision nodes, conditional routing, and checkpointed pause/resume. The tradeoff: a less mature ecosystem with fewer operational tools.

### Host-Level Podman for Code Execution

Run implementation tasks in rootless Podman containers on the Worker host instead of Kubernetes jobs or remote VMs. This simplifies the container lifecycle but requires Podman on every Worker host.

### Workflow Separation by Issue Type

Three separate LangGraph workflow definitions (Feature, Bug, Task Takeover) rather than one parameterized workflow. Each has fundamentally different planning stages. Shared implementation/CI/review nodes are reused across all three.

### Human Approval Gates

Workflows pause at defined gates and wait indefinitely for human approval. The `forge:yolo` label provides an opt-in escape hatch for autonomous operation. The tradeoff: increased latency for every ticket.

## Known Limitations

- **No PEL reclaim**: Unacknowledged messages from crashed workers remain in Redis PEL indefinitely. Recovery requires manual `XCLAIM`.
- **No distributed per-ticket lock**: Multiple workers can process events for the same ticket concurrently, causing potential checkpoint conflicts.
- **Ingress delivery is at-least-once**: webhook and poller observations are
  deduplicated and classified by the reconciliation ledger, but the gateway
  may still enqueue a retried transport message before the worker records it.
- **Webhook signature validation is optional**: Endpoints accept unsigned payloads when secrets are not configured.
- **No approval gate timeout**: Paused workflows wait indefinitely with no escalation.
- **Single Redis dependency**: No Sentinel, Cluster, or HA. Redis is a single point of failure.
- **Container security hardening gaps**: No `--cap-drop ALL`, `--no-new-privileges`, or `--read-only` root filesystem.
- **No cross-stream ordering**: Jira and GitHub streams are consumed independently with no ordering guarantee.

## Workflow Lifecycles

`>>` marks human approval gates. Gates are auto-approved when the `forge:yolo` label is set. For detailed node-level flows, see the [Feature](../guide/feature-workflow.md), [Bug](../guide/bug-workflow.md), and [Task](../guide/task-workflow.md) workflow guides.

### Feature Lifecycle

```mermaid
flowchart TD
    A["Ticket created\n(Feature/Story)"] --> B["Generate PRD"]
    B --> C[">> PRD approval"]
    C --> D["Generate technical spec"]
    D --> E[">> Spec approval"]
    E --> F["Decompose into epics"]
    F --> G[">> Plan approval"]
    G --> H["Generate tasks"]
    H --> I[">> Task approval"]
    I --> J["Route tasks by repo"]

    J --> K["Implement in container"]
    K --> L["Review and open PR"]
    L --> M["CI repair loop"]
    M --> N[">> Human code review"]
    N -->|merged| O["Aggregate status\nComplete"]
```

### Bug Lifecycle

```mermaid
flowchart TD
    A["Ticket created\n(Bug)"] --> B["Triage check"]
    B -->|"missing info"| C[">> Ask reporter"]
    C --> B
    B -->|"sufficient"| D["Root cause analysis"]
    D --> E[">> Present fix options\n(user selects >option N)"]
    E --> F["Generate fix plan"]
    F --> G[">> Plan approval"]

    G --> H["Implement in container"]
    H --> I["Review and open PR"]
    I --> J["CI repair loop"]
    J --> K[">> Human code review"]
    K -->|merged| L["Post-merge summary\nComplete"]
```

### Task Lifecycle

```mermaid
flowchart TD
    A["Ticket created\n(Task/Epic)"] --> B["Triage check"]
    B -->|"missing context"| C[">> Ask for details"]
    C --> B
    B -->|"sufficient"| D["Generate plan"]
    D --> E[">> Plan approval"]

    E --> F["Implement in container"]
    F --> G["Review and open PR"]
    G --> H["CI repair loop"]
    H --> I[">> Human code review"]
    I -->|merged| J["Complete"]
```
