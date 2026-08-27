# Observation contract

Forge accepts webhook and `forge-poller` deliveries through the same ingress
adapters. Both paths are normalized to the versioned `Observation` record
(`schema_version: "1.0"`). The strict contract contains source,
provider/resource identity, provider revision, observation times, normalized
facts, correlation metadata, and an optional evidence reference. Unknown
fields and future schema versions are rejected at the boundary.

## Identity

`observation_id` identifies the provider event record. It is generated from
the provider event ID plus provider/resource identity and revision context; it
does not include `source`. Replays that preserve the provider event ID retain
the same observation ID. When a poller must use a different transport ID,
`delivery_identity` still remains stable whenever the provider revision is
present.

`delivery_identity` is the deduplication identity used by Forge's observation
ledger. It is generated from source system, resource identity, and
`resource_revision`. Therefore webhook and poller deliveries of one provider
revision have the same delivery identity even when their transport event IDs,
received times, or source values differ. `revision_order` is ordering metadata
and is not included when a provider revision is available. For resources with
no revision, the provider event identity is used; callers must provide
`correlation.provider_event_id` (or `transport_event_id`) in that case.

The Jira adapter derives revisions from the immutable comment ID, the issue's
`updated` timestamp, or a changelog fingerprint. Source-control adapters use
the change-request head SHA, check state scoped to its commit, or immutable
comment/review IDs. The poller can therefore forward its existing Jira and
GitHub payloads; Forge assigns the source-independent identity before ledger
processing. Command IDs likewise use `delivery_identity`, so a poller retry
cannot create a second command/effect for a webhook-delivered revision.

An observation without a native provider revision is deliberately limited: it
is deduplicated only when its provider event ID is stable, and a later
revision cannot be ordered safely. Forge records such input as an
operator-visible conflict rather than guessing which external state is newer.
This is the explicit no-native-revision limitation, not a workflow checkpoint.

The shared fixtures at
[`github_pull_request_revision.json`](../../tests/contracts/fixtures/observations/github_pull_request_revision.json)
and
[`source_control_sequence.json`](../../tests/contracts/fixtures/reconciliation/source_control_sequence.json)
show equivalent source-control revisions and replay behavior. Contract tests
cover both ingress markers, cross-source deduplication, monotonic stale/reorder
handling, command identity, and the Jira revision derivation cases.

The companion poller changes preserve GitHub review/comment IDs and head SHAs,
include Jira issue `updated` values and comment IDs/timestamps in forwarded
payloads, and make synthetic delivery IDs replay-stable. The poller contract
tests cover those payload guarantees; Forge's adapter tests then verify their
identity and reconciliation semantics.
