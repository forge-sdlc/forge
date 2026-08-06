# Testing

## Running the Test Suite

```bash
# Fast PR suites
make test-pr

# Redis-backed component integration tests
make test-integration

# Deterministic end-to-end smoke tests
make test-e2e

# Specific test file
uv run pytest tests/unit/test_workflow.py -v

# With coverage
make coverage
```

Integration tests use `FORGE_TEST_REDIS_URL` when set and otherwise start a
pinned Redis Stack testcontainer. CI sets `FORGE_REQUIRE_TEST_REDIS=1`, so a
missing infrastructure dependency fails the gate instead of silently skipping it.

## Linting and Type Checking

Before submitting a PR, these must all pass:

```bash
make lint
uv run mypy src/forge/       # type check
```

## Payload-Based Testing

Test workflow stages without live webhooks using sample payloads from `tests/payloads/`.

```bash
# Trigger a Jira issue-created event
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/jira-feature-created.json

# Trigger a GitHub PR review
curl -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -d @tests/payloads/github-pr-approved.json
```

See the [Developer Guide](../developer-guide.md#6-testing-with-payloads) for the full set of payloads and how to trigger specific workflow stages.

## Testing CI Skip Commands

Post a PR comment via the GitHub API:

```bash
curl -X POST \
  "https://api.github.com/repos/your-org/your-repo/issues/123/comments" \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "/forge skip-gate e2e-openstack"}'
```

Then trigger a check run webhook to see Forge re-evaluate CI with the skip applied.

## Debugging

### Snapshot and Restore Checkpoints

```bash
# Capture current workflow state
uv run forge snapshot --ticket PROJ-123

# Restore to a previous snapshot
uv run forge restore --ticket PROJ-123 --snapshot snapshots/PROJ-123-2024-01-01.json
```

### Inspect Redis State

```bash
redis-cli -p 6380

# List all workflow checkpoints
KEYS forge:checkpoint:*

# View a specific checkpoint
GET forge:checkpoint:PROJ-123
```

### Worker Logs

The worker logs each node execution with the ticket key and node name. Use these to trace where a workflow stopped:

```bash
uv run forge worker 2>&1 | grep "PROJ-123"
```

See the [Developer Guide](../developer-guide.md#10-debugging-tools) for patching checkpoints directly and other advanced debugging tools.

## End-to-End Smoke Testing

Forge provides an automated end-to-end smoke test CLI command to verify that state checkpointing, the container runtime sandbox, model provider API connectivity, and the Deep Agent system are fully functional.

```bash
uv run forge smoke-test
```

This command runs completely offline from Jira/production branches, performing all actions in a disposable workspace:
1. **Diagnostic Graph Execution**: Compiles a 2-node LangGraph and executes it with the configured Redis state checkpointer, verifying workflow resumption.
2. **Workspace Setup & Container Verification**: Launches a rootless Podman environment inside a temporary folder with a mocked Git tree.
3. **Container Agent Execution & Verification**: Spawns the real agent inside the container, instructs it to create a specific verification file, and asserts that the file is successfully populated.
