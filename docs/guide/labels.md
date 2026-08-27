# Jira Labels

Forge uses Jira labels to track workflow state and trigger transitions. Labels are the primary way humans communicate approval decisions back to Forge.

## Workflow Labels

These labels advance the pipeline. Forge watches for label changes via Jira webhooks.

### Feature Workflow

| Stage | Pending Label | Approved Label |
|-------|--------------|----------------|
| PRD | `forge:prd-pending` | `forge:prd-approved` |
| Spec | `forge:spec-pending` | `forge:spec-approved` |
| Epic Plan | `forge:plan-pending` | `forge:plan-approved` |
| Tasks | `forge:task-pending` | `forge:task-approved` |

### Bug Workflow

| Stage | Label | Set by | Purpose |
|-------|-------|--------|---------|
| Triage | `forge:triage-pending` | Forge | Ticket is missing required fields; waiting for reporter to update |
| RCA Option Gate | `forge:rca-pending` | Forge | RCA posted with fix options; waiting for `>option N` selection |
| Plan Approval Gate | `forge:plan-pending` | Forge | Plan posted; waiting for approval |
| Plan Approval Gate | `forge:plan-approved` | Human | Approve plan and trigger task decomposition + implementation |

### Task Workflow

Standalone Tasks and Epics can be processed with the standard `forge:managed` label. These tickets bypass the standard parent Feature validation and use the Task workflow.

| Stage | Pending Label | Approved Label | Purpose |
|-------|--------------|----------------|---------|
| Triage | `forge:task-triage-pending` | _N/A_ | Standalone ticket is missing actionable context; waiting for an update |
| Plan Approval | `forge:plan-pending` | `forge:plan-approved` | Plan is posted; waiting for approval |
| Implementation | _N/A_ | _N/A_ | Approved plan is implemented in an isolated workspace, then reviewed and opened as a PR |
| CI + Review | _N/A_ | _N/A_ | CI is evaluated, failures are fixed, and the PR pauses for human review |

## Control Labels

| Label | Purpose |
|-------|---------|
| `forge:managed` | Marks the ticket for Forge automation. Add this when creating a ticket to start the workflow. |
| `forge:managed:task` | Identity preservation label used during Task Takeover transitions. |
| `forge:managed:task-takeover` | Identity preservation label used during Task Takeover transitions. |
| `forge:blocked` | Set by Forge when a stage fails. Forge posts a comment with the error. |
| `forge:retry` | Add this to resume from the exact node that failed, or to transition from `review_response_gate` back to `human_review_gate` (clearing contested review comments). Forge removes it after resuming. |
| `forge:yolo` | Auto-approve supported planning gates. Human PR review still remains a gate. |
| `repo:<owner>/<repo>` | Identifies repositories selected for planning and implementation. |

## How to Use Labels

**Starting a workflow:** Create a Jira issue and add `forge:managed`. Forge detects the issue type and begins the appropriate pipeline: Feature/Story, Bug, or standalone Task/Epic takeover.

**Approving a stage:** When Forge posts a PRD, spec, or other artifact, it sets the `forge:*-pending` label. Change it to `forge:*-approved` to advance the workflow. Do not add the approved label manually before Forge posts — it won't be recognized until the pending state is set.

**Requesting revisions:** Start a comment with `!` followed by your feedback. Forge regenerates the artifact and resets the pending label.

**Asking questions:** Start a comment with `?` or `@forge ask`. Forge answers without advancing or regenerating.

**Informational comments:** Comments without a recognized prefix (`!`, `?`, `@forge ask`, `>option`) are ignored by the workflow — use them for team discussion without triggering Forge.

**Handling failures:** When `forge:blocked` appears, read the Forge comment for the error. Fix the underlying issue if needed, then add `forge:retry`.

**Resetting contested reviews:** If the workflow is paused at `review_response_gate` due to contested comments, adding `forge:retry` will transition the workflow back to `human_review_gate`, clearing the contested comments and resetting the review state to await a fresh review.

!!! warning "Don't remove `forge:managed`"
    Removing `forge:managed` won't stop an in-progress workflow. It only prevents new workflows from starting on the ticket.

## Automatic Jira Status Transitions

While Forge primarily tracks internal state using workflow labels (to remain compatible with any Jira configuration), it also automatically transitions actual Jira issue statuses at key workflow milestones.

Forge utilizes the standard Jira workflow statuses (`In Progress` and `Closed`) to transition tickets as they move through implementation and delivery:

### ⚙️ Workspace Setup & Implementation Start
When workspace setup begins and code implementation starts, Forge automatically transitions the following issues to **`In Progress`**:
* **Tasks:** Any decomposed task tickets associated with the workflow.
* **Epics:** Any decomposed epic tickets associated with the workflow.
* **Parent Epic:** If the Feature or Bug ticket is linked to a parent Epic, that parent Epic is also automatically transitioned to **`In Progress`** (errors are gracefully caught and suppressed if the status transition is restricted in your project).

### ✅ Completion & Merge
When implementation is complete and the pull requests are successfully merged:
* **Tasks:** Associated task sub-tickets are transitioned to **`Closed`**.
* **Epics:** Associated epic sub-tickets are transitioned to **`Closed`** once all of their underlying tasks are completed.
* **Feature:** The main Feature ticket is transitioned to **`Closed`**.
* **Parent Epic:** The parent Epic of the Feature/Bug is automatically transitioned to **`Closed`** once all child tickets are merged and completed.
