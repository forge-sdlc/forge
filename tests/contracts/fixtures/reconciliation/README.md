# Reconciliation conformance fixtures

`source_control_sequence.json` is a provider-independent sequence of two
observations for one change request. It is intentionally JSON so the Forge
tests and `forge-poller` tests can consume the exact same revisions without
importing one project's Python package.

The `provider_event_id`, `resource_revision`, and `revision_order` values are
part of the observation contract. Transport delivery metadata (webhook versus
poller) is not part of the fixture and must not change the resulting command.
