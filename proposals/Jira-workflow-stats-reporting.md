# Proposal: Workflow Statistics & Automated Reporting

**Author:** Eran Kuris
**Date:** 2026-05-14
**Status:** In-Progress

## Summary

When a Forge workflow reaches a terminal state — successful completion, blocked, or unrecoverable failure — the system should automatically post a development statistics summary as a Jira comment on the Feature ticket. The primary quality signal is iteration count: how many manual interventions and corrections were needed at each stage. When a workflow is blocked due to escalation to human, the full stats summary is posted so the team can assess the work Forge completed before hitting the block. If token consumption also exceeds a configurable threshold, a cost alert is included. Users can request stats on demand for any ticket via `/forge stats` (Jira comment command). Additionally, a weekly status report aggregates statistics across all active and completed tickets per project, delivered via admin CLI.

## Motivation

### Problem Statement

Forge orchestrates the full SDLC — from PRD generation through implementation, CI, and review — but produces no summary of the process once a workflow completes. Teams have no automated way to evaluate how efficiently the pipeline performed:

- Number of manual interventions and corrections at each stage.
- Amount of revision cycles prior to approval.
- Token cost of the entire workflow.
- Which stages consistently require the most rework.
- Overall throughput & quality of their automated pipeline.

Without this data, stakeholders have no insight into how well the workflow is functioning, and engineers can't identify systemic bottlenecks.

### Current Workarounds

Teams manually correlate Jira comment history and Langfuse traces to recreate timelines. There is no rollup for weekly status. Someone has to manually aggregate tickets together. It's slow and prone to error. 

## Proposal

### Overview

Three complementary features:

1. **Per-ticket summary** — a Jira comment posted automatically when the workflow completes successfully, populated with information about iteration counts at each stage, stage durations, tokens used, links to PRs & CI attempts. When a workflow is blocked due to escalation to human, the full stats summary is always posted. If token usage also exceeds a configurable threshold (`FORGE_COST_ALERT_THRESHOLD`), a cost alert section is included.
2. **On-demand stats** — users can request current stats for any feature/bug ticket via `/forge stats` (Jira comment command). Returns the same summary format as the per-ticket summary, reflecting the current state of the workflow regardless of completion status.
3. **Weekly status report** — aggregated view across all tickets per project with activity during the reporting period, delivered via admin CLI command with email delivery to stakeholders.

All of these features pull data from LangGraph's checkpoint state, which is augmented with statistic fields that are recorded during execution of the workflow.

### Detailed Design

#### 1. State Schema: `StatsState` Mixin

A new `StatsState` TypedDict mixin added to `workflow/base.py`, following the existing pattern of `PRIntegrationState`, `CIIntegrationState`, and `ReviewIntegrationState`:

```python
class StatsState(TypedDict, total=False):
    stage_timestamps: dict[str, dict[str, str]]
    revision_counts: dict[str, int]
    token_usage: dict[str, int]
    stage_token_usage: dict[str, dict[str, int]]
    workflow_outcome: str | None
```

| Field | Shape | Purpose |
|-------|-------|---------|
| `stage_timestamps` | `{"prd": {"start": "...", "end": "..."}, ...}` | Duration of active Forge execution per stage |
| `revision_counts` | `{"prd": 2, "spec": 1, "review": 3, ...}` | Number of iterations/corrections per stage (including review cycles) |
| `token_usage` | `{"input": 12345, "output": 6789}` | Aggregate token consumption |
| `stage_token_usage` | `{"prd": {"input": ..., "output": ...}, ...}` | Per-stage token breakdown |
| `workflow_outcome` | `"completed" \| "blocked" \| "failed"` | Terminal status for reporting |

**Iteration tracking as quality signal:** The primary quality metric is the number of manual interventions and corrections at each stage. Fewer revision cycles mean Forge is producing higher-quality artifacts that humans can approve with minimal rework. When Forge output quality is high, the development process is more streamlined. Tracking iteration counts across stages — PRD revisions, spec revisions, plan revisions, CI fix attempts, review rounds — gives teams a direct measure of how well Forge is performing and where to focus improvements.

`FeatureState` and `BugState` both add `StatsState` to their inheritance chain. `create_initial_feature_state` and `create_initial_bug_state` get default values for all new fields. Both workflow types get identical stats collection and per-ticket summary treatment.

Tracked stages: `prd`, `spec`, `plan`, `task_generation`, `implementation`, `ci`, `review`.

#### 2. Stats Instrumentation

A utility module `workflow/stats/helpers.py` provides helper functions that nodes call to record data points:

```python
def record_stage_start(state: dict, stage: str) -> dict    # stage duration start
def record_stage_end(state: dict, stage: str) -> dict      # stage duration end
def record_tokens(state: dict, stage: str, input_tokens: int, output_tokens: int) -> dict
def increment_revision(state: dict, stage: str) -> dict
```

Each returns the updated state dict with the stats fields modified. Node instrumentation is minimal — one or two calls per node:

| Node | Instrumentation |
|------|----------------|
| `generate_prd` | `record_stage_start("prd")` at entry, `record_stage_end("prd")` + `record_tokens` at completion |
| `regenerate_prd` | `increment_revision("prd")` + `record_tokens` |
| `generate_spec` | `record_stage_start/end("spec")` + `record_tokens` |
| `regenerate_spec` | `increment_revision("spec")` + `record_tokens` |
| `decompose_epics` | `record_stage_start/end("plan")` + `record_tokens` |
| `regenerate_all_epics`, `update_single_epic` | `increment_revision("plan")` + `record_tokens` |
| `generate_tasks` | `record_stage_start/end("task_generation")` + `record_tokens` |
| `implement_task` | `record_stage_start("implementation")` on first task across all repos, `record_stage_end` when the last task in the last repo completes (spans workspace setup/teardown), `record_tokens` per task (container token data read from `.forge/metrics.json` after container exit) |
| `evaluate_ci_status`, `attempt_ci_fix` | `record_stage_start/end("ci")` + `record_tokens` |
| `human_review_gate` → review cycle | `record_stage_start/end("review")`, `increment_revision("review")` per cycle |

Token capture: extracted from LLM response objects that carry `input_tokens` and `output_tokens` in `response_metadata`. For direct Anthropic API calls, this comes from `response.usage`. For Vertex AI (ChatAnthropicVertex), usage metadata is available via LangChain's `response_metadata` on the returned message objects.

#### 3. Per-Ticket Summary Comment

A new module `workflow/stats/summary.py` with a `post_workflow_summary(state)` function that:

1. Reads stats fields from the checkpoint state
2. Formats a Jira wiki markup comment
3. Posts it to the Feature ticket via `JiraClient.add_comment()`

**Triggers:**

- `aggregate_feature_status` — sets `workflow_outcome = "completed"`, calls `post_workflow_summary(state)`. Always posts the full summary on successful completion.
- `escalate_to_blocked` — when the block reason is "escalated to human", always posts the full stats summary so the team can see exactly what Forge completed before hitting the block (stages reached, iterations, tokens consumed). If `token_usage["input"] + token_usage["output"]` also exceeds the configurable threshold (`FORGE_COST_ALERT_THRESHOLD`, default: 50000 tokens), a cost alert section is appended to the summary highlighting the token investment.

**On-demand stats (`/forge stats` command):**

Users can post `/forge stats` as a comment on any Jira ticket to get the current stats summary, regardless of workflow status. The response is posted as a Jira comment on the ticket, using the same summary format and reflecting the latest checkpoint state.

**Comment format:**

```
{panel:title=Forge Workflow Summary|borderStyle=solid}

*Outcome:* (/) Completed  |  *Total Duration:* 3h 34m
*Ticket:* PROJ-123  |  *Langfuse:* [View Session|https://langfuse.example.com/sessions/PROJ-123]

h4. Stage Overview
|| Stage || Duration || Iterations || Tokens (in/out) ||
| PRD Generation | 8m 12s | 0 | 2,340 / 1,890 |
| Spec Generation | 12m 04s | 1 | 4,120 / 3,450 |
| Epic Decomposition | 6m 30s | 0 | 3,200 / 2,800 |
| Task Generation | 4m 15s | 0 | 1,800 / 1,200 |
| Implementation | 2h 45m | — | 18,500 / 12,300 |
| CI Validation | 18m 20s | — | 1,200 / 900 |
| Human Review | — | 2 | — |

h4. Execution Details
* *PRs Created:* [PR #42|https://github.com/...], [PR #43|https://github.com/...]
* *CI Fix Attempts:* 1
* *Total Iterations:* 4 (across all stages)
* *Tasks Completed:* 5/5
* *Total Tokens:* 31,160 input / 22,540 output

{panel}
```

The Langfuse session link is generated from the ticket key (Langfuse groups traces by `session_id=ticket_key`). Stages that were never reached (e.g., Human Review on a blocked ticket) show "—" for all columns. This makes it immediately clear where the workflow stopped.

For blocked or failed outcomes, the header shows `(x) Blocked` or `(x) Failed` with the `last_error` message included.

#### 4. Weekly Status Report

A new module `workflow/stats/weekly_report.py` that aggregates data across all checkpoints with activity in the reporting window, scoped per project.

**Data collection:** Scans LangGraph checkpoints in Redis using existing `list_checkpoints` + `get_checkpoint_state` helpers, filtered by project. For each checkpoint, includes it if any `stage_timestamps` entry or `updated_at` falls within the reporting window.

**Report structure:**

| Section | Content |
|---------|---------|
| Summary | Ticket counts by status, total tokens, total iterations |
| Completed Tickets | Per-ticket row: key, title, duration, tokens, iterations |
| In Progress | Per-ticket row: key, title, current stage, elapsed time |
| Blocked | Per-ticket row: key, title, blocked stage, duration, error summary |
| Iteration Analysis | Most revised stage, CI first-pass rate, avg iterations per completed ticket |
| Token Consumption | Totals and percentage breakdown by stage |

#### 5. Report Delivery

**Weekly report — CLI (admin-only, requires Redis access):**
```bash
forge weekly-report --project PROJ               # stdout, last 7 days, scoped to project
forge weekly-report --project PROJ --days 14     # custom window
forge weekly-report --project PROJ --output report.md   # file export
forge weekly-report --format json --output r.json       # JSON for tooling
```

Scheduling is configured by the admin — a cron job or CI pipeline invokes the CLI command on a weekly cadence.

### User Experience

**Per-ticket summary — automatic, no user action required:**

When a workflow completes successfully, the team sees a structured summary comment appear on the Jira ticket. When a workflow is blocked due to escalation to human, the full stats summary is posted — if the token usage threshold is also exceeded, a cost alert is included. Jira's built-in notification system handles email delivery to watchers, reporter, and assignee automatically when the comment is posted.

**On-demand stats — any time, any ticket:**

```
/forge stats

# CLI:
$ forge stats PROJ-123
```

Both return the current stats snapshot for the ticket regardless of workflow status.
Post this as a Jira comment on any ticket to get the current stats snapshot regardless of workflow status.

**Weekly report — admin CLI (on-demand or scheduled):**

```bash
$ forge weekly-report --project PROJ

== Forge Weekly Status Report ==
Project: PROJ  |  Period: 2026-05-07 → 2026-05-14

SUMMARY
  Completed: 4 tickets  |  In Progress: 3  |  Blocked: 1
  Total tokens: 245,800 in / 178,200 out
  Avg. iterations (completed): 1.2 per ticket

COMPLETED TICKETS
  PROJ-101  Feature X         3h 42m   31k tokens   0 iterations
  PROJ-108  Feature Y         5h 10m   48k tokens   2 iterations
  PROJ-112  Bug Z             1h 05m   12k tokens   0 iterations
  PROJ-115  Feature W         6h 50m   52k tokens   1 iteration

IN PROGRESS
  PROJ-120  Feature A         at: Implementation    elapsed: 2h 30m
  PROJ-122  Feature B         at: Spec Approval     elapsed: 1d 4h
  PROJ-125  Bug C             at: CI Validation     elapsed: 45m

BLOCKED
  PROJ-118  Feature D         at: CI Validation     blocked: 2d
  → CI fixes exhausted after 5 attempts (lint-check, type-check)

ITERATION ANALYSIS
  Most revised stage: Spec — 1.5 avg iterations
  CI fix rate: 3/4 tickets passed CI on first attempt
  Avg. total iterations per ticket: 1.2

TOKEN CONSUMPTION
  Total: 245,800 in / 178,200 out
  By stage: Implementation 62% | Spec 14% | PRD 10% | Plan 8% | Other 6%
```

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Query Langfuse for all stats at report time | Keeps checkpoint state lean; Langfuse already has token data | Adds external API dependency during reporting; stage durations not tracked in Langfuse; weekly aggregation requires many API calls | Self-contained checkpoint data is simpler and more reliable |
| Persist stats to a separate Redis store | Decouples reporting from workflow state; flexible schema | Two sources of truth; extra write path; must keep in sync with checkpoint | Unnecessary complexity when checkpoint already persists per-node |
| LangGraph callback middleware for auto-instrumentation | Nodes stay untouched; new nodes auto-tracked | LangGraph node-level callback API is limited; token usage happens inside nodes, not at graph level; adds indirection | Still requires per-node token capture; net-more complex |
| Decorator-based `@track_stage` on node functions | Clean separation of concerns; explicit opt-in | Async decorator + TypedDict state interaction is fragile; still need token passthrough | Pragmatically harder than inline calls for marginal benefit |
| Post weekly report as Jira comments on tickets | All data in one place | Floods individual tickets with aggregate data irrelevant to that ticket; stakeholders must still visit each ticket | Weekly report is for stakeholders, not per-ticket context |

## Implementation Plan

### Phases

1. **Phase 1: State schema + helpers** — Add `StatsState` mixin, implement `workflow/stats/helpers.py`, add defaults to initial state functions. (~0.5 day)
2. **Phase 2: Node instrumentation** — Add `record_stage_start/end`, `record_tokens`, `increment_revision` calls to all generation, regeneration, implementation, CI, and review nodes. (~1 day)
3. **Phase 3: Per-ticket summary** — Implement `workflow/stats/summary.py`, wire into `aggregate_feature_status`, format Jira comment. (~0.5 day)
4. **Phase 4: Weekly report engine** — Implement `workflow/stats/weekly_report.py` with checkpoint scanning, aggregation, and report formatting. (~1 day)
5. **Phase 5: CLI command** — Add `forge weekly-report` subcommand with `--days`, `--output`, `--format` flags. (~0.5 day)
6. **Phase 6: Weekly report email delivery** — Implement email sending for weekly status report to stakeholders. (~0.5 day)
7. **Phase 7: Tests** — Unit tests for helpers, summary formatting, report aggregation. Integration tests for CLI command. (~1 day)

### Dependencies

- [ ] LLM response objects must expose `input_tokens` / `output_tokens` (available via Anthropic API responses and Vertex AI via LangChain `response_metadata`)
- [ ] Redis checkpointer must support scanning all checkpoints (existing `list_checkpoints` helper)
- [ ] Email delivery credentials for weekly report (SMTP or Gmail API)

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Token usage not consistently available from all LLM call paths (direct API vs agent vs container) | Low | Med | Orchestrator captures tokens from API responses; container writes `.forge/metrics.json` with its token data; use 0 as fallback for any path that doesn't expose usage |
| Checkpoint scanning for weekly report is slow with many tickets | Low | Med | `list_checkpoints` already limits results; add date-based filtering to avoid scanning old checkpoints |
| Stats fields increase checkpoint size in Redis | Low | Low | Fields are small (timestamps, integers); negligible compared to existing `messages` and `context` fields |

## References

- Design draft: `docs/superpowers/specs/2026-05-13-workflow-stats-reporting-design.md`
- Existing Prometheus metrics: `src/forge/api/routes/metrics.py`
- Langfuse tracing integration: `src/forge/integrations/langfuse/tracing.py`
- LangGraph checkpoint system: `src/forge/orchestrator/checkpointer.py`
- Workflow terminal nodes: `src/forge/workflow/nodes/human_review.py`
- Related proposal: [PR #31](https://github.com/forge-sdlc/forge/pull/31) — Revision Summary on Artifact Approval (covers per-stage revision history posted on approval; this proposal tracks revision counts as part of broader workflow stats, not as standalone approval-time summaries)
