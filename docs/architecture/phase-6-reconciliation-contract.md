# Reconciliation contract

Webhook and polling delivery are interchangeable inputs to Forge.
Workflow position remains owned by the workflow instance; an external
observation may update an external-state projection but cannot set
`current_node`, workflow identity, or transition counters.

## Conformance contract

Every ingress source must provide a versioned `Observation` with:

- `source`: `webhook` or `poller` (transport metadata only);
- provider/resource identity (`source_system`, `resource`, and
  `resource_revision`);
- a stable provider event identity in `correlation.provider_event_id`;
- monotonic `revision_order` whenever a provider revision cannot be ordered
  from its native identifier; and
- provider facts that are identical for equivalent revisions.

`Observation.delivery_identity` intentionally excludes `source`, so equivalent
webhook and poller deliveries deduplicate. A command may be evaluated only for
an accepted observation. Duplicate, stale, or conflicting deliveries are
recorded for inspection and cannot create another external effect.

The shared fixture is
[`source_control_sequence.json`](../../tests/contracts/fixtures/reconciliation/source_control_sequence.json).
It is JSON rather than a Python fixture so Forge and `forge-poller` can replay
the same provider revisions. Forge's contract tests cover equivalent envelopes
and replay the sequence with a lost first revision, duplicate cross-source
delivery, stale reordered delivery, and a duplicate replay. The resulting
latest revision, accepted command, and effect list must match the clean replay.

Run the Forge side with:

```shell
pytest -q tests/contracts/reconciliation tests/unit/reconciliation
```

## Poller companion behavior and limitation

`forge-poller` remains an independently deployable delivery source. Its
webhook-shaped Jira and GitHub payloads preserve the provider IDs and revision
metadata needed by Forge's normalizing adapters, which assign the
source-independent revision and delivery identity before workflow evaluation.
Poller cursors are delivery optimizations only; they are not read or written
as workflow checkpoints.
Consequently a lost, repeated, or reordered delivery cannot move workflow
position or create a duplicate command/effect.

The provider limitation is explicit: if a payload contains neither a native
revision nor a stable provider event ID, Forge cannot establish ordering. It
records the observation but classifies a competing update as
`operator_required`; it does not infer chronology from receipt time or a
poller cursor. Jira payloads lacking `issue.fields.updated`, changelog items,
or a comment ID are in this category and require the provider/poller to add a
stable revision before automatic convergence is possible.
