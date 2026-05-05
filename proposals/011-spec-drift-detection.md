# Proposal: Spec Drift Detection and Jira Sync After Review Implementation

**Author:** eshulman2
**Date:** 2026-05-05
**Status:** Draft

## Summary

When `implement_review` addresses PR reviewer feedback, the resulting code changes may deviate from the original approved specification — either because the reviewer asked for something outside the spec's scope, or because an agreed change implies a different design than what the spec described. Currently, `spec_content` in workflow state and the spec stored in Jira become silently stale once review-driven changes land. This proposal introduces a post-review spec drift check: after `implement_review` pushes changes, a lightweight agent comparison detects whether the diff materially diverges from the spec, and if so, updates the Jira spec to reflect the as-built reality and notifies stakeholders.

## Motivation

### Problem Statement

The spec is the agreed contract between the human stakeholder and Forge. It is approved at the `spec_approval_gate` before any implementation begins. After approval, three events can cause the implementation to silently diverge from that contract:

1. **PR reviewer requests an out-of-spec change.** A reviewer asks for a different API shape, an added field, or a reworked flow that wasn't in the spec. `implement_review` implements it. The code is now correct by the reviewer's lights but wrong by the spec's.

2. **PR reviewer requests a design change that contradicts the spec.** The spec said "store session tokens in Redis"; the reviewer says "use JWT, no server-side storage". The agent implements JWT (and rightly contests or implements per `implement_review` logic), but if it implements, the spec still says Redis.

3. **CI fix commits architectural changes.** The CI fix path (`attempt_ci_fix`) can commit non-trivial changes — dependency replacements, interface renames, approach pivots — to get tests passing. The spec doesn't know about these.

In all three cases, the spec_content in state and in Jira describes a system that is no longer what Forge built. A future reviewer reading the Jira ticket, or Forge itself revisiting the ticket for a follow-on task, will operate on a stale contract.

### Current Workarounds

None. The spec is never updated after `spec_approval_gate`. Developers must manually compare the final PR diff to the spec, notice the drift, and decide whether to amend the spec themselves. This rarely happens.

## Proposal

### Overview

After `implement_review` pushes commits to the branch, run a lightweight spec-drift check:

1. Compute `git diff origin/main..HEAD` (the full branch diff, not just the last push).
2. Pass the diff and the current `spec_content` to a Claude call that classifies the delta as **in-spec** or **drifted**.
3. If drifted:
   - Generate a revised spec reflecting the as-built behavior.
   - Update the spec in Jira (same storage path as the original: comment, custom field, or attachment).
   - Post a brief Jira comment summarizing what changed and why.
   - Update `spec_content` in workflow state.
4. If in-spec: no action, log and continue.

The check runs only when there are new commits (same condition used for the existing post-change review and PR description sync).

### Detailed Design

#### Where it runs

In `implement_review`, after the push succeeds and `sync_pr_description` completes:

```
implement_review:
  ├── [push with new commits]
  ├── run_post_change_review      (existing)
  ├── sync_pr_description         (existing)
  └── check_spec_drift            (new)
      ├── in-spec  → continue to wait_for_ci_gate
      └── drifted  → update_spec_in_jira → continue to wait_for_ci_gate
```

It also runs after `attempt_ci_fix` on the same condition (new commits pushed). This covers the CI architectural-pivot scenario.

#### New helper: `check_and_sync_spec`

```python
async def check_and_sync_spec(
    workspace_path: str,
    ticket_key: str,
    spec_content: str,
    branch_name: str,
    current_repo: str,
) -> str | None:
    """Compare branch diff to spec. Returns updated spec text if drifted, else None."""
```

Steps inside:
1. Run `git diff origin/main..HEAD` to get the full branch diff.
2. Call Claude with:
   - The current spec
   - The full branch diff
   - Instruction: classify as in-spec or drifted; if drifted, produce an updated spec that reflects the actual implementation while preserving any requirements still met as originally specified.
3. Parse the structured response (`VERDICT: IN_SPEC` / `VERDICT: DRIFTED\n<updated spec>`).
4. If drifted, return the updated spec text. Otherwise return `None`.

#### Jira update

Reuse the same conditional write path from `generate_spec` / `regenerate_spec_with_feedback`:

```python
if settings.jira_store_in_comments:
    await jira.add_structured_comment(ticket_key, "Technical Specification (Post-Review Update)", updated_spec, comment_type="spec")
elif settings.jira_spec_custom_field:
    await jira.update_custom_field(ticket_key, settings.jira_spec_custom_field, updated_spec)
else:
    # Replace attachment
    await jira.delete_attachments_by_name(ticket_key, f"{ticket_key}-spec.md")
    await jira.add_attachment(ticket_key, filename=f"{ticket_key}-spec.md", content=updated_spec, content_type="text/markdown")
```

#### Jira notification comment

```
Spec updated to reflect review-driven changes.

The following areas diverged from the approved specification:
- [brief summary from the Claude response]

The updated spec is now stored and reflects the as-built implementation.
```

#### New state field

```python
class ReviewIntegrationState(TypedDict, total=False):
    # existing ...
    spec_drift_detected: bool  # True if spec was updated post-review
```

#### Prompt sketch

```
You are reviewing whether a set of code changes diverge from an approved specification.

## Approved Specification
{spec_content}

## Branch Diff (full, from main)
{branch_diff}

## Task
Classify the diff as IN_SPEC or DRIFTED.

DRIFTED means the implementation:
- Adds behavior that is not described in the spec
- Changes a design decision that the spec explicitly made (e.g., different storage backend, different API shape)
- Removes behavior the spec requires

Do NOT classify as DRIFTED for:
- Implementation details the spec left unspecified (internal function names, helper structure, test strategy)
- Bug fixes or error handling improvements consistent with spec intent
- Style or formatting changes

If IN_SPEC:
Output exactly: VERDICT: IN_SPEC

If DRIFTED:
Output: VERDICT: DRIFTED
Then a one-paragraph summary of what drifted.
Then the full updated specification that accurately reflects the as-built implementation.
Preserve spec sections that are still accurate. Only update sections that diverged.
```

### User Experience

**In-spec (common case):**
No visible change. Workflow proceeds to `wait_for_ci_gate` as today.

**Drift detected:**
```
[Forge, on AISOS-452]
Spec updated to reflect review-driven changes.

The reviewer requested that session token validation use asymmetric JWT
(RS256) instead of the HMAC-based token store described in the spec.
The spec has been updated to reflect the JWT approach now in use.

Updated specification is attached.
```

The Jira ticket now has an accurate spec. A future maintainer or Forge run reading the ticket sees what was actually built.

### Scope

This proposal covers `implement_review` and `attempt_ci_fix`. It does **not** cover changes made by the initial `implement_task` node — those are assumed to be in-spec by construction. If the implementation itself deviates from the spec, the `local_review` node and code review gate should catch it; spec-drift detection is not the right tool for that path.

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Always regenerate the full spec from the diff | Produces a maximally accurate spec | Expensive; loses approved requirements that weren't touched | Spec sections not touched by review should be preserved as approved |
| Let the reviewer manually update the spec in Jira | No Forge complexity | Never happens in practice; spec stays stale | Human behavior is the current workaround — this is the problem |
| Gate on a new approval step: "Spec updated, please re-approve" | Keeps humans in the loop | Adds friction to every non-trivial review | Most drift is benign; re-approval loop would stall workflows constantly |
| Detect drift only for contested comments that were confirmed | Targets the highest-risk drift | Misses uncontested out-of-spec additions | A reviewer asking for something outside the spec doesn't trigger contesting |
| Update spec after every push (not just review-driven) | Comprehensive | Too broad; initial implementation push should track the spec, not rewrite it | The problem is specific to review-driven divergence |

## Implementation Plan

### Phases

1. **Phase 1:** `check_and_sync_spec` helper + Claude prompt for drift classification (~half day)
2. **Phase 2:** Wire into `implement_review` post-push path; add `spec_drift_detected` state field; Jira update and comment (~half day)
3. **Phase 3:** Wire into `attempt_ci_fix` post-push path (~1 hour)
4. **Phase 4:** Unit tests for drift classification logic; integration test with fixture diff and spec (~half day)

### Dependencies

- [ ] `JiraClient` already has the spec write paths (`add_structured_comment`, `update_custom_field`, `add_attachment`, `delete_attachments_by_name`) — no new client methods needed
- [ ] `ForgeAgent` needs a new `classify_spec_drift(spec, diff)` method (or use the direct Anthropic client with a one-shot call)
- [ ] `workspace/git_ops.py` needs a `diff_from_main()` method if one doesn't already exist

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Claude over-classifies benign implementation details as drift | Med | Med | Prompt explicitly excludes implementation-detail changes; tune with test fixtures |
| Large diffs exceed context window for drift check | Low | Med | Truncate diff to changed files most relevant to the spec (filter by file extension or path) |
| Drift check adds latency to the already-long review implementation path | Low | Low | Claude call is a single fast completion; no container spin-up; acceptable |
| Updated spec conflicts with a pending Q&A thread in `qa_history` | Low | Med | Clear `qa_history` for `spec` entries when spec is updated post-review |

## Open Questions

- [ ] Should the drift check also run after the human approves a contested comment and `implement_review` runs again? (Likely yes — the confirmed-out-of-spec change is the highest-risk case.)
- [ ] If drift is detected on a bug workflow (where there is no spec, only an RCA), should we update the RCA description instead? Or skip the check entirely for bugs?
- [ ] Should the Jira notification include a side-by-side diff of the old vs new spec, or is a prose summary sufficient?
- [ ] Should `spec_drift_detected: True` in state cause any downstream behavioral change (e.g., the next review gate is more strict), or is it purely informational?
