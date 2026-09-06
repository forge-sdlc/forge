# Runtime internals

This page is the component-level reference. Read [Control-plane architecture](control-plane.md)
first for the system ownership model and the safe extension paths.

## State and correctness boundaries

Forge deliberately keeps four kinds of durable state separate:

| Record | Authority | Purpose |
| --- | --- | --- |
| Observation ledger | External resource revisions | Deduplicate, order, and classify webhook and poller evidence |
| Workflow checkpoint | Forge process instance | Pin the definition and retain process position and station state |
| Effect journal | Forge external-write intent | Make mutations recoverable and idempotent across crashes |
| Execution timeline | Operational history | Explain observations, transitions, attempts, effects, and operator actions |

External facts do not directly overwrite process position. An accepted observation is interpreted
as a command, validated, and applied through the selected workflow's transition policy. Conversely,
a checkpoint does not claim ownership of Jira issue content, pull-request state, or CI results; a
new provider revision can cause those facts to be reconciled and re-evaluated.

## Delivery and concurrency

Queue delivery is at least once. The observation `delivery_identity` makes equivalent webhook and
poller deliveries converge before command handling. The ledger uses monotonic provider revisions
and records duplicate, stale, conflict, and accepted decisions. Where a provider supplies no stable
revision, Forge requires a stable event identity and reports ambiguity instead of guessing.

Workflow state is persisted through LangGraph's Redis checkpointer. Definitions are pinned by
revision and digest, so publication or activation of a newer revision cannot silently change an
in-flight run. Compatibility analysis and explicit migration mappings govern intentional moves.

## Station execution

A graph node projects the permitted checkpoint fields into a versioned station request. The station
returns a typed outcome; a reducer validates and applies only the fields that station owns. The same
request can run through the local station runner without Redis, LangGraph, or provider clients,
except where the station's declared capability explicitly requires an adapter.

Agent operations resolve a model connection through stage policy and declared capabilities such as
`tools` and `structured_output`. Structured stages preserve the full Deep Agent tool loop, validate
the final object, and retry with a tool-based schema strategy when native structured output fails.

## External effects and recovery

Required external mutations are stable `EffectCommand` values. Forge records an intent before
calling Jira or source control, leases execution, and stores attempt history and provider evidence.
Reprocessing the same logical action reuses its idempotency identity. Indeterminate and failed
effects are visible through the operator API and can be replayed without rerunning the whole station.

The operator endpoints expose:

- `GET /api/v1/workflows/{ticket_key}/execution`
- `GET /api/v1/workflows/{ticket_key}/execution/timeline`
- `GET /api/v1/effects/workflow/{run_id}`
- `POST /api/v1/effects/{idempotency_key}/replay`

These views do not advance workflow state.

## Security boundaries

- Webhook signatures are validated when the corresponding secret is configured; production must
  configure both Jira and source-control secrets.
- Operator execution/effect routes require their configured bearer token and fail closed when the
  token is absent.
- Rootless Podman constrains implementation execution with configured CPU, memory, network, and
  timeout limits.
- Containers do not receive Jira, Redis, or source-control credentials. Those writes pass through
  the host-side durable effect boundary.
- Custom workflow definitions select registered capabilities; they cannot embed credentials,
  provider-specific calls, arbitrary HTTP, shell code, or Python imports.
