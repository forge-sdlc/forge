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

## Control Labels

| Label | Purpose |
|-------|---------|
| `forge:managed` | Marks the ticket for Forge automation. Add this when creating a ticket to start the workflow. |
| `forge:blocked` | Set by Forge when a stage fails. Forge posts a comment with the error. |
| `forge:retry` | Add this to resume from the exact node that failed. Forge removes it after resuming. |

## How to Use Labels

**Starting a workflow:** Create a Jira issue and add `forge:managed`. Forge detects the issue type (Feature or Bug) and begins the appropriate pipeline.

**Approving a stage:** When Forge posts a PRD, spec, or other artifact, it sets the `forge:*-pending` label. Change it to `forge:*-approved` to advance the workflow. Do not add the approved label manually before Forge posts — it won't be recognized until the pending state is set.

**Requesting revisions:** Start a comment with `!` followed by your feedback. Forge regenerates the artifact and resets the pending label.

**Asking questions:** Start a comment with `?` or `@forge ask`. Forge answers without advancing or regenerating.

**Informational comments:** Comments without a recognized prefix (`!`, `?`, `@forge ask`, `>option`) are ignored by the workflow — use them for team discussion without triggering Forge.

**Handling failures:** When `forge:blocked` appears, read the Forge comment for the error. Fix the underlying issue if needed, then add `forge:retry`.

!!! warning "Don't remove `forge:managed`"
    Removing `forge:managed` won't stop an in-progress workflow. It only prevents new workflows from starting on the ticket.
