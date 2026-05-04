# Proposal: S3-Compatible Artifact Storage for `.forge/` and Workspace Cleanup

**Author:** eshulman2  
**Date:** 2026-05-13  
**Status:** Draft

## Summary

Replace per-worker ephemeral `.forge/` directories with a shared S3-compatible object store, and enforce deterministic workspace cleanup after push. Workers download the current `.forge/` contents before starting work and upload after completing it. The bucket is named deterministically from the ticket key and repo name (O(1) lookup, no coordination required), giving each (ticket, repo) pair its own isolated bucket. This matters for multi-repo workflows — a feature touching three repos creates three buckets, and each worker knows exactly which bucket owns its `.forge/` directory before downloading. Buckets are deleted when the workflow ends.

---

## Motivation

### Problem Statement

The current design has two unresolved issues:

1. **`.forge/` is not shared across workers.** Each worker writes task files, CI failure reports, and review plans to a local `.forge/` directory. When a second worker picks up the same ticket, it cannot see what the first one wrote, forcing redundant re-fetching and making multi-stage pipelines fragile (e.g., `implement_review` cannot read `review-plan.md` written by an earlier analysis step if the workspace was recreated).

2. **Workspace directories are not cleaned up reliably.** Development directories frequently persist after a workflow completes because containers run as root (or a different user) and leave files the host cannot delete without elevated permissions. This wastes disk space and causes state confusion on restart.

### What is not changing

Push-rejection (non-fast-forward pushes from stale workspaces) is already addressed by `pull_rebase` in `prepare_workspace()`. That fix remains in place and is not affected by this proposal.

### Design constraint

CI logs, must-gather archives, and other large raw artifacts are **never** stored in `.forge/` or in LangGraph state. Workers store only structured summaries and key excerpts. Large artifacts are referenced by URL or path only. This keeps `.forge/` buckets small regardless of workflow complexity.

---

## Proposal

### Overview

Each ticket gets a dedicated S3 bucket with a name derived deterministically from its ticket key. Workers download the bucket contents into `.forge/` at startup and upload the `.forge/` directory back to the bucket after completing their work. When the workflow ends (PR merged or escalated), the bucket is deleted. The workspace directory is force-deleted after every push using `podman unshare` to handle root-owned files from containers.

### Detailed Design

#### Bucket naming

Bucket names are derived from the ticket key and the repo name:

```
forge-{ticket_key.lower().replace("_", "-")}-{repo_name.lower().replace("_", "-")}
```

Examples:
- `AISOS-525` + `my-service` → `forge-aisos-525-my-service`
- `OCPBUGS-12345` + `api-gateway` → `forge-ocpbugs-12345-api-gateway`

Each bucket is unique to a **(ticket, repo) pair**. This is required for multi-repo workflows: a feature that touches `api-gateway` and `frontend` creates two separate buckets. Each worker knows which bucket owns its `.forge/` directory from the ticket key and the repo it is assigned to — no registry or coordination is needed.

Single-repo workflows (all bug fixes, most tasks) produce exactly one bucket, identical in behavior to the original per-ticket design.

S3 bucket name constraints (3–63 chars, lowercase, hyphens only) are satisfied by this scheme for all standard Jira project key and repo name formats. Combined names longer than 59 characters would need truncation with a suffix hash; this is documented as an edge case.

#### Worker lifecycle

```
Worker starts (knows ticket_key + repo_name)
    ↓
forge_artifact_store.download(ticket_key, repo_name, workspace_path / ".forge")
    ↓  (no-op if bucket does not exist yet)
[container runs, reads/writes .forge/]
    ↓
forge_artifact_store.upload(ticket_key, repo_name, workspace_path / ".forge")
    ↓
Worker ends
```

Download is a no-op if the bucket does not exist (first worker on this ticket+repo). Upload creates the bucket if it does not exist. Both operations are best-effort and non-blocking for read-only workers (analysis containers that write nothing may skip the upload).

For multi-repo workflows, each worker operates on its own (ticket, repo) bucket independently. Workers on different repos for the same ticket never share a bucket and cannot interfere with each other's `.forge/` state.

#### `ForgeArtifactStore`

A new thin client wraps the S3-compatible API:

```python
class ForgeArtifactStore:
    def __init__(self, settings: Settings) -> None: ...

    async def download(self, ticket_key: str, repo_name: str, target_dir: Path) -> None:
        """Download all objects in the (ticket, repo) bucket into target_dir/.forge/."""

    async def upload(self, ticket_key: str, repo_name: str, source_dir: Path) -> None:
        """Upload all files in source_dir/.forge/ to the (ticket, repo) bucket."""

    async def delete_bucket(self, ticket_key: str, repo_name: str) -> None:
        """Delete the (ticket, repo) bucket and all its contents."""

    async def delete_all_buckets(self, ticket_key: str) -> None:
        """Delete all buckets for a ticket across all repos (called on workflow end)."""
```

Configuration:

```python
# Settings additions
FORGE_ARTIFACT_STORE_ENDPOINT: str = ""      # S3-compatible endpoint (empty = disabled)
FORGE_ARTIFACT_STORE_BUCKET_PREFIX: str = "forge"
FORGE_ARTIFACT_STORE_ACCESS_KEY: str = ""
FORGE_ARTIFACT_STORE_SECRET_KEY: str = ""
```

When `FORGE_ARTIFACT_STORE_ENDPOINT` is empty, `ForgeArtifactStore` is a no-op stub. This means the feature is opt-in and existing deployments are unaffected.

#### Bucket lifecycle

| Event | Action |
|-------|--------|
| First worker starts on a (ticket, repo) | Bucket created on first upload |
| Subsequent workers on same (ticket, repo) | Download → work → upload |
| PR merged (`post_merge_summary`) | `delete_all_buckets(ticket_key)` — removes all repo buckets for the ticket |
| Workflow escalated / blocked permanently | `delete_all_buckets(ticket_key)` |
| Buckets not deleted on workflow end (failure) | Cleaned up by scheduled job (TTL: 14 days) |

#### Workspace force-cleanup after push

After every successful push, the workspace directory is force-deleted using `podman unshare` to handle files created inside containers as root:

```python
async def force_delete_workspace(workspace_path: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "podman", "unshare", "rm", "-rf", workspace_path
    )
    await proc.wait()
    logger.info(f"Force-deleted workspace: {workspace_path}")
```

This is called:
- In `teardown_workspace` (already exists; currently uses `shutil.rmtree` which fails on root-owned files)
- After push in `implement_bug_fix`, `implement_review`, and `attempt_ci_fix` if the workspace is not being reused immediately

`workspace_path` is cleared from state after deletion so subsequent workers know to recreate it.

---

## Known Issues and Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Concurrent workers upload simultaneously, last write wins | Low (sequential by design) | Med | Upload only when `.forge/` was modified; document as known limitation for concurrent workers |
| S3 endpoint unavailable at worker start | Low | Med | Log warning, proceed without `.forge/` state (first-worker semantics) |
| Bucket not deleted on abnormal workflow end | Med | Low | Scheduled cleanup job with 14-day TTL |
| `podman unshare` not available in all environments | Low | Med | Fall back to `shutil.rmtree`; log warning on failure |
| Ticket key + repo name produces invalid bucket name (>59 chars combined) | Very Low | Low | Truncate to 50 chars + 8-char hex hash suffix |

---

## Tech Debt: Jira-editable workflow artifacts

Workflow artifacts (PRD, spec, RCA, plan) are written to both LangGraph state and Jira. Human edits made directly in Jira (e.g., editing the Epic description after a PRD is generated) are not propagated back to state. Forge may overwrite these edits on the next regeneration.

**Decision:** Not implementing Jira→state sync at this time. The read-modify-write pattern (reading current Jira content before regenerating) partially mitigates this, but a full sync mechanism is deferred. Tracked as tech debt.

The full design for this — including delimiter-based field mapping, checksum-based change detection, and lazy sync at worker startup — is documented as a future proposal.

---

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Shared NFS / filesystem volume | Simple for single-host; no API calls | Operationally heavy for multi-host; filesystem mounts add deployment complexity | Object storage works identically on single-host (MinIO) and multi-host (S3/GCS) without mounts |
| Keep per-worker workspaces + `pull_rebase` | No infrastructure change | `.forge/` not shared; cleanup remains unreliable | Current state; acceptable short-term but blocks multi-stage artifact passing |
| Git for `.forge/` state | Auditable, replicable | Extremely complex; merge conflicts on every task | Over-engineered |
| Store artifacts in LangGraph state (Redis) | Already available | CI logs and large artifacts explode state size; Redis not designed for binary blobs | State is for structured workflow data; files belong in object storage |

---

## Implementation Plan

1. Add `ForgeArtifactStore` client and config options (stub when endpoint not configured)
2. Add download call at the start of `ContainerRunner.run()` (before container starts)
3. Add upload call after `ContainerRunner.run()` returns
4. Replace `shutil.rmtree` in `teardown_workspace` with `force_delete_workspace()`
5. Add `delete_bucket()` call in `post_merge_summary` and `escalate_blocked`
6. Add scheduled cleanup job for orphaned buckets (TTL: 14 days)
7. Tests: unit tests for `ForgeArtifactStore` (mocked S3); integration test with local MinIO

---

## Open Questions

- [ ] Should uploads be synchronous (blocking worker) or fire-and-forget?
- [ ] Should the bucket be per-ticket or per-ticket-per-run (to support rollback to a previous run's `.forge/` state)?
- [ ] For the cleanup job: should it run inside the Forge worker process or as a separate service?
- [ ] Should `.forge/task.json` be excluded from uploads (it is written fresh each run)?
