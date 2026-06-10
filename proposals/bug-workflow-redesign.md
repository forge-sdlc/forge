# Proposal: Bug Workflow Redesign — Triage, Container Analysis, Reflection, and Planning

**Author:** eshulman2  
**Date:** 2026-05-12  
**Status:** Under Review

## Summary

The current bug workflow generates an RCA from the Jira ticket description alone, without exploring the codebase, and goes directly to implementation after a single approval gate. This proposal redesigns it with five new stages: a triage check that ensures the ticket has enough information before analysis begins, a container-based analysis agent that explores repos and produces a structured RCA with 1–4 fix options, a reflection loop that validates the RCA using a critic agent, an option selection gate where the user picks a fix approach, and a planning stage that produces an approved implementation plan before any code is written. Multi-repo fixes are decomposed into linked Jira tasks, reusing the existing task execution infrastructure.

---

## Motivation

### Problem Statement

The current bug workflow has three fundamental weaknesses:

1. **No triage.** The workflow starts immediately from whatever the reporter typed into Jira. Under-specified tickets — missing stack traces, reproduction steps, affected versions — go into analysis and produce low-quality or incorrect RCAs. There is no mechanism to ask the reporter for what's missing.

2. **Analysis without code exploration.** `analyze_bug` sends the Jira description and summary to an agent with no repo access. The RCA is generated from the bug report alone, which means it can only reflect what the reporter described — not what the code actually does. Root causes that require reading the implementation are missed.

3. **No planning before implementation.** After RCA approval the workflow goes directly to `implement_bug_fix`. There is no opportunity to align on how the fix should be structured, which files it touches, or how it should be tested before the container starts writing code. For multi-repo bugs there is no decomposition at all.

### Current Workarounds

Engineers either write highly detailed bug reports (putting all the context that the agent would need into the description), or they accept low-quality RCAs and provide manual feedback through the revision gate. Multi-repo bugs are handled by manually creating tasks and not using Forge for them.

---

## Proposal

### Overview

Replace the current single-gate RCA flow with a five-stage pipeline:

```
triage_check → [triage_gate if needed] → analyze_bug → reflect_rca
             → rca_option_gate → plan_bug_fix → plan_approval_gate
             → decompose_plan → [existing task execution loop]
```

Everything from `setup_workspace` onward is unchanged, with the exception of `local_review`, which is enhanced for the bug workflow — see [Implementation Phase Enhancement](#implementation-phase-enhancement) below.

### Detailed Design

#### Full graph

```
route_entry
    ↓
triage_check ─── sufficient ──→ analyze_bug ←──────────────────────┐
  ↑     │                            ↓                              │ (feedback)
  │     └── missing ──→ triage_gate  reflect_rca ──── gaps ──→ analyze_bug
  │                      (pause)          ↑               (max 3 iterations)
  └──────────── resume ──────────┘    passes
                                          ↓
                                   rca_option_gate (pause)
                                     │
                        ┌────────────┴────────────┐
                    >option N                 feedback
                        ↓                        ↓
                  plan_bug_fix           regenerate_rca → analyze_bug loop
                        ↓
                  plan_approval_gate (pause, forge:plan-approved label)
                        │
               ┌────────┴────────┐
           approved           feedback
               ↓                  ↓
         decompose_plan    regenerate_plan → plan_bug_fix
               ↓
        [linked Jira tasks]
               ↓
        setup_workspace → implement_bug_fix → local_review
            → create_pr → teardown_workspace
            → ci_evaluator → human_review_gate → post_merge_summary → END
```

#### Stage 1: Triage

**`triage_check`** (no container) posts an immediate Jira comment acknowledging receipt and stating that analysis is underway, then evaluates the ticket against six required fields:

1. Steps to reproduce
2. Expected vs actual behavior
3. Environment (OS, runtime, infrastructure)
4. Affected versions
5. Error output (stack trace, log snippet, or error message)
6. Affected component / repo

The checklist is fixed and not configurable per project. Fields that are genuinely not applicable (e.g., no stack trace for an infra bug with no error output) count as satisfied if the ticket makes clear why they don't apply.

If all six are present or clearly inferable, routes directly to `analyze_bug` — no pause.

If any are missing, posts a targeted Jira comment naming only the absent fields, sets `forge:triage-pending`, and routes to `triage_gate`. On resume, `triage_check` re-evaluates the updated ticket (description + all comments). Repeats until satisfied. No iteration limit.

#### Stage 2: Analysis

**`analyze_bug`** runs in a standard container (same `ForgeAgent` infra as `implement_bug_fix`, full write permissions inside the container, repo access). Receives the full Jira ticket, `triage_missing_fields`, and any `reflection_critique` from a previous iteration.

The agent explores the codebase — clones repos, checks out branches, reads files, inspects git history — to locate the defect. The `analyze-bug.md` prompt instructs the agent to follow a hypothesis-driven investigation methodology:

1. **Form explicit hypotheses** — before reading code, list candidate root causes ranked by likelihood given the bug report.
2. **Test each hypothesis against the code** — read the relevant code paths, not just the most plausible one. Eliminate candidates explicitly: record which hypotheses were ruled out and why. This reasoning chain is visible to the human at `rca_option_gate` and gives `reflect_rca` something concrete to validate beyond structural checks.
3. **Enumerate complete state spaces** — when the bug involves states, phases, conditions, or branching paths, search the codebase to identify *all* possible values or code paths rather than assuming. This prevents partial fixes that address some cases but miss others.

The resulting RCA must include:
- Confirmed code location (file, function, line range)
- Mechanism of failure
- Trace from trigger to symptom
- **Hypothesis log**: candidates considered, evidence checked, reason each was accepted or eliminated
- **When the bug was introduced**: the specific commit and PR (via `git blame` and history inspection) that caused the regression, or "unknown" if history is ambiguous. Surfaces at `rca_option_gate` as evidence the agent found the correct code path.
- **Confidence level**: High / Medium / Low with a percentage (e.g., "Medium — 65%") and a one-sentence rationale. Not an escalation trigger — a quality signal for the human at `rca_option_gate` and a concrete input for `reflect_rca` (a low-confidence RCA that hasn't exhausted its hypotheses is a gap worth looping on).
- **1–4 distinct fix options**, each with title, description, and trade-offs
- Embedded code snippets sufficient for the critic to validate without independent exploration
- **Reproducibility assessment**: whether the bug can be demonstrated by a unit or integration test in isolation, and if not, why (e.g., requires a running cluster, environment-specific state, specific infra). If a unit-level test is feasible, the agent includes the full source of a minimal failing test in the RCA output — the test is not committed, but the implementing agent uses it as a specification. If not, it documents the conditions under which the bug manifests so a human can verify independently.

**`reflect_rca`** runs in a standard container (same infra) because validating that named files and functions exist at the stated locations requires repo access. Receives the RCA text. Validates:
- Named files and functions exist at the stated locations
- Failure mechanism is actually possible given the code
- Fix options are genuinely distinct
- No unexplained gaps between trigger and symptom
- **Hypothesis coverage**: multiple hypotheses were considered and the hypothesis log documents why each was accepted or eliminated — not just the first plausible match
- **Historical grounding**: `git blame` or commit history was consulted and an introduction point is recorded (or explicitly marked unknown)
- **Confidence level**: a confidence level is present; if Low or Medium, the gaps identified in the rationale are concrete enough to loop on

Outputs `VALID` or a structured critique listing specific gaps. On gaps: stores critique in `reflection_critique`, routes back to `analyze_bug`. Max 3 iterations. After the third failed reflection, the best available RCA is used and a warning note is appended to the Jira comment.

#### Stage 3: RCA Option Gate

**`rca_option_gate`** pauses and posts a structured Jira comment presenting the fix options. Sets `forge:rca-pending` (reuses existing label).

```
## Root Cause Analysis
<rca summary>

## Fix Options

**Option 1: <title>**
<description and trade-offs>

**Option 2: <title>**
<description and trade-offs>

...

Reply with `>option N` to select an approach, or comment with feedback to revise the RCA.
```

Comment routing on resume:

| Comment | Action |
|---------|--------|
| `>option N` (case-insensitive) | Validate N in range → store `selected_fix_option` + `selected_fix_approach` → `plan_bug_fix` |
| `>option N` out of range | Post clarifying comment → re-pause |
| No `>option` prefix | RCA feedback → `regenerate_rca` → re-runs `analyze_bug` + `reflect_rca` → return to gate |
| Question (Q&A mode) | `answer_question` → return to gate |

#### Stage 4: Planning

**`plan_bug_fix`** runs in a standard container with repo access. Receives the full RCA and `selected_fix_approach`. Produces a concrete implementation plan: which files to change, what the changes accomplish, new tests required, order of operations, and which repos are involved. Posts the plan as a Jira comment and sets `forge:plan-pending`.

```
## Implementation Plan

**Approach: <selected option title>**

### Changes
1. `path/to/file.py` — <what changes and why>
2. `path/to/test_file.py` — <new regression test>
...

### Repos
- `repo-name` (tag: repo:repo-name)

### Order of operations
<step-by-step sequence>
```

**`plan_approval_gate`** pauses. Sets `forge:plan-pending` label.

| Trigger | Action |
|---------|--------|
| `forge:plan-approved` label applied | Route to `decompose_plan` |
| Plain comment | Feedback → `regenerate_plan` → re-runs `plan_bug_fix` → return to gate |

Event dispatch (label vs. comment) follows the same routing pattern as the existing `rca_option_gate` — the worker inspects the incoming webhook event type and branches accordingly.

**`decompose_plan`** (no container) creates one Jira **Task** per repo, linked to the bug ticket via "implements" issue link. Each task gets a `repo:<repo-name>` label (same pattern as epic decomposition in the feature workflow) and scoped implementation instructions. Every approved plan produces at least one linked task — if no repo is explicitly identified, the task is created against the primary repo from the ticket context. There is no hard cap on the number of tasks; the planning prompt instructs the agent to keep the decomposition proportionate to the scope of the fix, mirroring the feature workflow's approach.


#### New state fields

```python
# Triage
triage_passed: bool
triage_missing_fields: list[str]

# Analysis / reflection
reflection_count: int
reflection_critique: str | None
rca_options: list[dict]          # [{title, description, tradeoffs}, ...]
reproducibility_assessment: str | None  # human-readable; includes failing test source if feasible (not committed)

# Option selection
selected_fix_option: int | None
selected_fix_approach: dict | None

# Planning
plan_content: str | None
linked_task_keys: list[str]      # Jira task keys created by decompose_plan

# Qualitative review (implementation phase)
local_review_verdict: str | None         # "adequate" | "tests_incomplete" | "symptom_only"
qualitative_feedback: str | None         # structured feedback from reviewer passed to next attempt
qualitative_retry_count: int             # number of re-implementation attempts made
qualitative_review_failed: bool          # True if cap was reached without adequate verdict
```

#### New `ForgeLabel` entries

```python
TRIAGE_PENDING = "forge:triage-pending"
PLAN_PENDING   = "forge:plan-pending"
PLAN_APPROVED  = "forge:plan-approved"
```

#### Prompts

| File | Status | Purpose |
|------|--------|---------|
| `triage-bug.md` | New | Evaluate ticket against six-field checklist; output missing fields or "sufficient" |
| `analyze-bug.md` | Rewrite | Container analysis with 1–4 fix options and embedded code evidence |
| `regenerate-rca.md` | Rewrite | Re-analyze incorporating user feedback; same output structure as `analyze-bug.md` |
| `reflect-rca.md` | New | Critic pass; output `VALID` or structured gaps |
| `plan-bug-fix.md` | New | Generate implementation plan from RCA + selected option |
| `regenerate-plan.md` | New | Revise plan incorporating user feedback |
| `fix-bug.md` | **Retired** | Replaced by `implement-task` prompt used with plan as context |
| `local-review-bug.md` | New | Bug-specific qualitative review using RCA + plan + diff; outputs `adequate`, `tests_incomplete`, or `symptom_only` verdict with specific guidance |
| `post-merge-summary.md` | New | Generate release note and fix summary from RCA + plan + implementation notes for Jira comment |

### Implementation Phase Enhancement

#### Enhanced `local_review` (bug workflow only)

The existing `local_review` node runs a container that inspects the diff and fixes mechanical breaking issues (linting, compilation errors, test failures). For the bug workflow it is enhanced with a bug-specific prompt (`local-review-bug.md`) that additionally receives the RCA, `selected_fix_approach`, and `plan_content` as context.

The container performs two checks in sequence:

1. **Mechanical check** (existing): run linters, type checkers, and the test suite; report any remaining issues.
2. **Qualitative check** (new): re-read the RCA and plan, then inspect the actual diff and ask:
   - Does the change address the confirmed root cause, or only a symptom?
   - Do the new or modified tests actually prove the bug is fixed (i.e., would they have caught this bug before the fix)?
   - Could someone break this fix without a test failing? (the most actionable test-adequacy question)
   - Does the diff match the scope of the approved plan, or has it drifted significantly?
   - **Completeness across call sites**: if the fix guards or wraps something in one location, are there similar patterns elsewhere in the codebase that need the same treatment?
   - **Backward compatibility / rollback safety**: can this change be reverted cleanly if needed?
   - **Security basics**: no secrets leaked in the diff, no injection vectors introduced, error messages don't expose internals.
   - **Bidirectional test validation**: for any regression test the agent wrote itself (not provided in the RCA), does the commit log or implementation notes confirm that the test was verified to fail without the fix and pass with it?

The reviewer is **read-only** — it never modifies files itself. All remediation is routed back to `implement_bug_fix` as structured feedback, so every change goes through the same review loop.

The qualitative check produces one of three verdicts stored in `local_review_verdict`:

| Verdict | Meaning | Action |
|---------|---------|--------|
| `adequate` | Fix addresses root cause; tests are sufficient | Proceed to `create_pr` |
| `tests_incomplete` | Fix looks correct but test coverage is weak | Increment `qualitative_retry_count`, route back to `implement_bug_fix` with specific guidance on what tests are missing |
| `symptom_only` | Fix addresses a symptom; root cause unresolved | Increment `qualitative_retry_count`, route back to `implement_bug_fix` with structured feedback on what was wrong and what the root cause still requires |

In both non-adequate cases the workspace is **not reverted**. The implementing agent receives the current code state alongside the reviewer's feedback and decides what to keep, discard, or change. A blanket revert would discard potentially useful work — test scaffolding, refactoring, partial progress — even when only the fix direction was wrong.

#### Bidirectional test validation

The `implement-task.md` prompt instructs the implementing agent that for any regression test it writes itself (i.e., not the pre-written failing test supplied in the RCA, which was already validated red), it must:

1. Write the test.
2. Temporarily revert the fix (leaving the test in place).
3. Confirm the test fails — if it passes without the fix, it is not catching the bug.
4. Re-apply the fix and confirm the test passes.
5. Note the outcome in the commit message (e.g., "Verified test fails without fix").

This scope — agent-written tests only — avoids the complexity of reverting a fix that may be structurally entangled with new symbols the test file references. The `local-review-bug.md` qualitative check verifies bidirectional validation was done by inspecting the commit log or implementation notes; absence of evidence is a flagged gap, not a hard block.

#### Re-implementation protocol

When `implement_bug_fix` is entered with a non-zero `qualitative_retry_count`, the prompt (`implement-task.md`) is augmented with a structured review-addressing block:

```
## Review Feedback (attempt {{ qualitative_retry_count }} of 2)

Verdict: {{ local_review_verdict }}

{{ qualitative_feedback }}

## Instructions for this attempt

You are not starting from scratch. The workspace already contains changes from the previous attempt.
Your job is to address the reviewer's feedback while preserving work that is still correct.

Before making changes:
1. Read `git diff main` to understand what the previous attempt changed.
2. Read the reviewer's feedback carefully — it identifies specific gaps, not a general failure.
3. Decide what to keep (correct changes, useful test infrastructure, refactoring),
   what to revert (the parts the reviewer identified as wrong), and what to add or replace.

Do not re-implement everything from scratch unless the reviewer explicitly indicated
the entire approach is wrong. Make targeted changes and explain your decisions in the commit message.
```

This ensures the implementing agent has full context — RCA, plan, current code state, and reviewer rationale — and is guided to make surgical changes rather than re-doing all the work. The loop is capped at **2 retries**. After the second failed attempt, the workflow does **not** escalate — it proceeds to `create_pr` with `qualitative_review_failed: true`. The PR description includes a clearly marked warning block summarising the unresolved verdict, the reviewer's final feedback, and the number of attempts made, so the human reviewer has full context and can decide whether to request changes or close the PR. The Jira ticket also receives a comment with the same information.

#### Release notes and post-merge summary

**PR description** — `create_pr` includes a release note section generated from the RCA, `selected_fix_approach`, and `plan_content`. The section is formatted to be directly usable in release documents:

```
## Release Note

**Component:** <component / repo>
**Fix:** <one-sentence description of what was fixed>
**Root cause:** <one-sentence summary of the root cause>
**Impact:** <who is affected and under what conditions>
```

**Post-merge Jira comment** — `post_merge_summary` is a new no-container node that fires after the PR is merged (the existing merge path through `human_review_gate`). It posts a comment to the bug ticket containing:

- A fix summary (what was changed and why, derived from the RCA and plan)
- The same release note block from the PR description, formatted as standalone text ready to paste into a release document

`post_merge_summary` runs after `human_review_gate` routes to END on the merge path. It is non-blocking: a failure to post the comment is logged and the workflow ends normally — it does not re-open the ticket or escalate.

### User Experience

**Happy path (well-specified bug, single repo, one obvious fix):**

```
[Bug filed with all six fields present]

[Forge, immediately]
Analyzing this bug — RCA and fix options will be posted here once complete.

[Forge, after analysis]
forge:rca-pending set on PROJ-123

## Root Cause Analysis
The session manager does not invalidate tokens on logout...

## Fix Options
**Option 1: Invalidate on logout**
Add token revocation in logout handler. Simple, targeted.

**Option 2: Short-lived tokens with refresh**
Switch to short-lived JWTs. More secure but larger change.

[Engineer]
>option 1

[Forge]
forge:plan-pending set on PROJ-123

## Implementation Plan
**Approach: Invalidate on logout**
1. `auth/session.py` — add revoke_token() call in logout()
2. `tests/test_auth.py` — add test for token invalid after logout
...

[Engineer applies forge:plan-approved]

[Forge creates PROJ-456: "Fix: Invalidate session token on logout (auth-service)"]
[Forge implements, opens PR, CI passes, ready for review]
```

**Under-specified bug:**

```
[Bug filed: "login is broken"]

[Forge, immediately]
Analyzing this bug — will post questions or RCA shortly.

[Forge]
forge:triage-pending set on PROJ-123

I need more information before I can analyze this bug:
- Steps to reproduce
- Expected vs actual behavior
- Environment (OS, runtime, infrastructure)
- Affected versions
- Error output (stack trace or error message)

[Reporter adds details]

[Forge resumes analysis...]
```

---

## Error Handling

All new nodes follow the existing pattern: exceptions are caught, stored in `last_error`, `retry_count` incremented. After max retries, `escalate_blocked` is called.

| Node | On failure | Special case |
|------|-----------|--------------|
| `triage_check` | Retry up to 3×, then escalate | — |
| `analyze_bug` | Retry up to 3×, then escalate | — |
| `reflect_rca` | Loop up to 3 iterations without `VALID` | After 3rd iteration: use best available RCA, append warning note to Jira comment, continue — do not escalate |
| `plan_bug_fix` | Retry up to 3×, then escalate | — |
| `decompose_plan` | Escalate immediately | No partial task creation — all tasks created atomically or not at all |
| `>option N` out of range | Post clarifying comment, re-pause | Not an escalation; no retry count incremented |

## Q&A Mode

The existing `answer_question` node is extended to cover all three new pause gates: `triage_gate`, `rca_option_gate`, and `plan_approval_gate`. A question comment at any of these gates routes to `answer_question` and returns to the originating gate, using the same `current_node` routing already in place for the RCA gate. No new Q&A infrastructure is required.

## Scope: What Is Not Changing

- `setup_workspace`, `implement_bug_fix`, `teardown_workspace` — unchanged
- `local_review` — enhanced for the bug workflow (see Implementation Phase Enhancement above)
- `create_pr` — enhanced to include a release note section in the PR description
- CI evaluation loop (`ci_evaluator`, `attempt_ci_fix`, `wait_for_ci_gate`) — unchanged
- Human review loop (`human_review_gate`, `implement_review`, `review_response_gate`) — unchanged; `implement_review` enhanced with bug context and decision log visibility; merge path extended with `post_merge_summary` before END
- `escalate_blocked` — unchanged, used by all new nodes on failure
- `route_entry` resume logic — extended to cover new nodes; all existing resume paths preserved

---

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Dedicated reproduction stage before analysis | Grounds RCA in observed behavior; catches already-fixed or non-reproducible bugs early | Full system reproduction is impossible for large distributed projects (e.g., OCP/OSP) that require a running cluster, specific infra, or environment state a container cannot provide | Replaced by a reproducibility assessment field in the RCA output: the agent writes a failing unit test when feasible, and documents reproduction conditions when not |
| Always pause at triage gate | Simple, uniform flow | Adds friction to well-specified bugs | Conditional gate keeps happy path fast |
| Reflection inside container (opaque loop) | Simpler graph | Not observable or resumable independently | Graph node is more debuggable and testable |
| Single combined triage + analysis container | Fewer container invocations | Can't resume from triage without re-running analysis; mixed responsibilities | Separate nodes have cleaner contracts |
| Subtasks instead of linked tasks for decomposition | Closer to parent-child semantics | Jira doesn't support subtasks on Bug issue type | Linked tasks with "implements" is the correct Jira model |
| Comment prefix for plan approval (`>approve`) | Consistent with option selection | Inconsistent with all other approval gates (label-based) | Labels are the existing pattern |

---

## Implementation Plan

### Phases

1. **Phase 1: Triage** — `triage_check` node, `triage_gate`, `triage-bug.md` prompt, new `TRIAGE_PENDING` label, `route_entry` extended for resume (~1 day)
2. **Phase 2: Container analysis** — Rewrite `analyze_bug` for repo exploration, `analyze-bug.md` and `regenerate-rca.md` rewrites, `rca_options` state parsing (~1.5 days)
3. **Phase 3: Reflection loop** — `reflect_rca` node, `reflect-rca.md` prompt, loop routing with max-iteration handling (~1 day)
4. **Phase 4: Option selection gate** — `>option N` comment parsing, `selected_fix_approach` state, updated `rca_option_gate` routing (~0.5 days)
5. **Phase 5: Planning** — `plan_bug_fix` node, `plan_approval_gate`, `regenerate_plan`, `plan-bug-fix.md` and `regenerate-plan.md` prompts, `PLAN_PENDING`/`PLAN_APPROVED` labels (~1.5 days)
6. **Phase 6: Decomposition** — `decompose_plan` node, Jira task creation with "implements" link, `repo:<name>` tagging, `linked_task_keys` state (~1 day)
7. **Phase 7: Enhanced local review** — `local-review-bug.md` prompt, `qualitative_feedback` state, re-implementation loop with cap, `qualitative_review_failed` PR warning block (~1 day)
8. **Phase 8: Release notes and post-merge summary** — release note block in `create_pr`, `post_merge_summary` node, `post-merge-summary.md` prompt, merge-path routing from `human_review_gate` (~0.5 days)
9. **Phase 9: Tests and cleanup** — Update `tests/flows/bug_workflow/`, retire `fix-bug.md`, extend `route_entry` for all new nodes, update Q&A routing to cover new gates (~1 day)

### Dependencies

- [ ] `ForgeAgent.run_task()` must support passing a workspace path for the analysis container (triage check does not need a workspace; analysis does)
- [ ] Jira client needs `create_issue_link()` for the "implements" link type used by `decompose_plan`
- [ ] `rca_options` parsing: structured output format must be defined in `analyze-bug.md` and consistently parseable by the orchestrator (JSON block or delimited sections)
- [ ] `forge:plan-approved` label must be registered in the worker's label-event routing table alongside existing approval labels

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Reflection loop runs 3× on every ticket, tripling analysis time | Med | Med | Cap at 3; monitor average iteration count; consider 2 as default |
| `analyze_bug` container produces inconsistently structured `rca_options` | High | High | Define strict output schema in prompt; add parsing validation with fallback |
| `triage_check` is too strict and blocks well-specified tickets | Low | Med | Prompt must use "clearly inferable" not "explicitly stated"; test against real ticket corpus |
| `decompose_plan` creates tasks with incorrect repo tags | Med | Med | Validate `repo:<name>` tags against known repos from project metadata before creating |
| Retiring `fix-bug.md` breaks any existing in-flight bug workflows | Low | High | `route_entry` resume routing preserves paths for existing checkpoints; retiring prompt only affects new invocations |

---

## References

- [Design doc](../docs/superpowers/specs/2026-05-12-bug-workflow-design.md)
- [Current bug workflow graph](../src/forge/workflow/bug/graph.py)
- [Current bug workflow nodes](../src/forge/workflow/nodes/bug_workflow.py)
- [Feature workflow epic decomposition](../src/forge/workflow/feature/graph.py) — repo tagging pattern reference
