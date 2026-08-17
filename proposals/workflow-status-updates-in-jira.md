# Proposal: Workflow Status Updates in Jira

**Author:** eshulman2
**Date:** 2026-04-19
**Status:** Implemented

## Summary

After a user approves tasks, Forge goes silent in Jira until the PR is created (or something fails). Implementation can run for hours inside containers, leaving the user with no indication of progress. This proposal adds lightweight, structured status comments to Jira at each meaningful phase transition so users can follow the workflow without leaving Jira.

## Motivation

### Problem Statement

From the user's perspective, the workflow has two Jira-visible states: "waiting for approval" and "PR created". Everything between those — workspace provisioning, task implementation, local code review, CI, and CI fix attempts — produces no Jira output. A user who approved tasks at 9am and checks Jira at 11am sees exactly what they saw at 9am. They have no way to know whether Forge is running normally, stuck, or done.

### Current Jira touchpoints (feature workflow)

| Phase | Jira update |
|-------|-------------|
| PRD generated | Comment with PRD content, sets `forge:prd-pending` |
| Spec generated | Comment/attachment with spec, sets `forge:spec-pending` |
| Epics created | Sets `forge:plan-pending` |
| Tasks created | Creates task issues, sets `forge:task-pending` |
| **task-approved → PR created** | **Silence** (hours can pass here) |
| PR created | Comment with PR URL |
| CI failure → blocked | Error comment, sets `forge:blocked` |
| Human review approved | Transitions tasks/epics/feature and parent Epic to Closed |

### Current Workarounds

Users must check container logs (`podman logs forge-AISOS-xxx-...`) or wait for the PR comment to appear. There is no way to know whether implementation is on task 1 of 5 or nearly done.

## Proposal

### Overview

Add structured status comments to the feature ticket and individual task tickets at each significant phase transition. Introduce a `forge:implementing` label to make the in-progress state machine-readable. Keep comments short and factual — this is a status feed, not a log dump.

### Detailed Design

#### New label

Add `forge:implementing` to `ForgeLabel`:

```python
IMPLEMENTING = "forge:implementing"
```

Set on the feature ticket when workspace setup begins; cleared (replaced) when PR is created or blocked.

#### Status comment helper

Add `jira_progress_comment(jira, ticket_key, message)` to a shared utility module (`forge/workflow/utils_jira.py` or inline in each node). Errors from these calls are logged and swallowed — a failed status comment must never block the workflow.

#### Per-phase updates

**1. Workspace setup starts** (`setup_workspace` node)
- Feature ticket: comment `"⚙️ Implementation starting for {repo}. Setting up workspace..."`
- Feature ticket: set label `forge:implementing`
- Each task, epic, and parent Epic ticket: transition to **In Progress** Jira status

**2. Task implementation starts** (`implement_task` node, per task)
- Task ticket: comment `"🔨 Forge is implementing this task."`

**3. Task implementation complete** (`implement_task` node, after each task)
- Task ticket: comment `"✅ Implementation complete. Running local code review before PR."`

**4. Local code review** (`local_review` node, first pass only)
- Feature ticket: comment `"🔍 Running local code review on changes before creating PR."`
- If a fix pass runs: `"🔧 Local review found issues, applying fixes (pass {n})."` 

**5. CI waiting** (`wait_for_ci_gate` node)
- Feature ticket: comment `"⏳ PR #{pr_number} created. Waiting for CI checks to complete."`

**6. CI fix attempt** (`attempt_ci_fix` node)
- Feature ticket: comment `"🔄 CI check failed. Attempting automated fix ({attempt}/{max_attempts})."`

**7. AI code review** (`ai_review` node)
- Feature ticket: comment `"🤖 CI passed. Running AI code review before requesting human review."`

**8. Human review ready** (already has PR URL comment — no change needed)

#### What NOT to add

- No comments for approval gates (already have label changes)
- No comments for Q&A answers (already commented as replies)
- No comments for retries/transient errors below the auto-retry threshold (noise)
- No comments for teardown (implementation detail, not user-relevant)

### User Experience

A user who approved tasks at 9am and checks Jira at 10:30am would see on the feature ticket:

```
[10:02] Forge: ⚙️ Implementation starting for org/repo. Setting up workspace...
[10:03] Forge: 🔨 Implementing task AISOS-391...     ← on the task ticket
[10:41] Forge: ✅ Implementation complete. Running local code review before PR.
[10:41] Forge: 🔍 Running local code review on changes before creating PR.
[10:42] Forge: Pull request created: https://github.com/...
[10:42] Forge: ⏳ PR #47 created. Waiting for CI checks to complete.
```

And on AISOS-391 (a task ticket):
```
[10:03] Forge: 🔨 Forge is implementing this task.
[10:41] Forge: ✅ Implementation complete. Running local code review before PR.
```

### Data flow

No new state fields are needed. All comments use data already present in state (`ticket_key`, `current_repo`, `implemented_tasks`, `current_pr_number`, `ci_fix_attempts`). Status updates are fire-and-forget inside each node before the node returns.

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Jira status transitions only (no comments) | Clean, machine-readable | Status fields are coarse; no detail about which phase or repo | Doesn't convey enough for multi-repo features |
| Webhook/push to external dashboard | Real-time, rich UI | Requires new infrastructure, out of scope | Too much infra work for the UX gain |
| Single "implementation started" comment only | Minimal noise | Doesn't help distinguish stuck vs. slow | The CI and fix-attempt updates are high-value |
| Log streaming to Jira attachment | Full fidelity | Jira is not a log viewer; too noisy | Comments are the right granularity |

## Implementation Plan

### Phases

1. **Phase 1: Core implementation comments** — `setup_workspace`, `implement_task`, `local_review` nodes. Add the `forge:implementing` label. Task-level In Progress transitions. (~1 day)
2. **Phase 2: CI and review comments** — `wait_for_ci_gate`, `attempt_ci_fix`, `ai_review` nodes. (~half day)

### Dependencies

- [ ] `ForgeLabel.IMPLEMENTING` added to `forge/models/workflow.py`
- [ ] Jira client supports transitioning tasks to "In Progress" (`transition_issue` already exists, need the status value)

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Jira API rate limiting from extra comments | Low | Med | Swallow errors, no retry; comments are best-effort |
| "In Progress" transition not available in all Jira projects | Med | Low | Catch `TransitionNotFound`, log and skip |
| Comment noise for long-running multi-task features | Low | Low | One comment per task per phase; manageable volume |

## Open Questions

- [ ] Should implementation comments go on the parent **feature** ticket, the individual **task** tickets, or both? Current proposal does both, but task tickets may be enough since they're linked.
- [ ] For the `forge:implementing` label: should it be set at the feature level only, or also on each task ticket individually?
- [ ] Should the CI fix-attempt comment include the failure summary (which checks failed) for faster human diagnosis if all retries exhaust?
