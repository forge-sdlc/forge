# Test Webhook Payloads

Sample Jira webhook payloads for testing the Forge workflow.

## Endpoint

```
POST http://localhost:8000/api/v1/webhooks/jira
Content-Type: application/json
```

## Usage with curl

```bash
# Start a new feature workflow
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/01-feature-created.json

# Request PRD revision
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/02-prd-revision-requested.json

# Approve PRD
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/03-prd-approved.json

# Request task revision (after plan approval)
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/08-task-revision-requested.json

# Approve tasks to start implementation
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/09-task-approved.json
```

## Workflow Sequence

| # | File | Action | Next State |
|---|------|--------|------------|
| 1 | `01-feature-created.json` | Create feature | PRD generated, `forge:prd-pending` |
| 2 | `02-prd-revision-requested.json` | `!` comment with feedback | PRD regenerated |
| 3 | `03-prd-approved.json` | Label change | Spec generated, `forge:spec-pending` |
| 4 | `04-spec-revision-requested.json` | `!` comment with feedback | Spec regenerated |
| 5 | `05-spec-approved.json` | Label change | Epics decomposed, `forge:plan-pending` |
| 6 | `06-plan-revision-requested.json` | `!` comment with feedback | Epics regenerated |
| 7 | `07-plan-approved.json` | Label change | Tasks generated, `forge:task-pending` |
| 8 | `08-task-revision-requested.json` | `!` comment with feedback | Tasks regenerated |
| 9 | `09-task-approved.json` | Label change | Implementation starts |

## Q&A Mode Payloads

Ask questions about generated artifacts without triggering regeneration:

| # | File | Description |
|---|------|-------------|
| 10 | `10-prd-question.json` | Question about PRD using `?` prefix |
| 11 | `11-spec-question.json` | Question about Spec using `?` prefix |
| 12 | `12-forge-ask-question.json` | Legacy question payload |
| 13 | `13-plan-question.json` | Question about Epic plan using `?` prefix |
| 14 | `14-task-question.json` | Legacy question payload |
| 15 | `15-rca-question.json` | Question about Bug RCA using `?` prefix |

```bash
# Ask a question about the PRD (stays paused, no regeneration)
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/10-prd-question.json

# Ask a question about the Spec
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/11-spec-question.json

# Legacy question payload
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/12-forge-ask-question.json

# Ask about epic plan structure
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/13-plan-question.json

# Ask about task testing strategy
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/14-task-question.json

# Ask about bug RCA (Bug workflow)
curl -X POST http://localhost:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d @tests/payloads/15-rca-question.json
```

Questions are detected by:
- Starting with `?` (e.g., `?Why did you choose this approach?`)

## Label Reference

| Stage | Pending | Approved |
|-------|---------|----------|
| PRD | `forge:prd-pending` | `forge:prd-approved` |
| Spec | `forge:spec-pending` | `forge:spec-approved` |
| Plan | `forge:plan-pending` | `forge:plan-approved` |
| Task | `forge:task-pending` | `forge:task-approved` |

### Control Labels

| Label | Purpose |
|-------|---------|
| `forge:managed` | Indicates ticket is managed by Forge |
| `forge:blocked` | Workflow blocked due to unrecoverable error |
| `forge:retry` | **Add to retry current stage** - clears errors and re-runs |

### Retrying Failed Workflows

When a workflow fails (e.g., clone timeout, API error), it will:
1. Post a comment tagging the reporter and assignee
2. Set the `forge:blocked` label

To retry:
1. Add the `forge:retry` label to the ticket
2. Forge will clear the error and retry from the current stage
3. The workflow will NOT go back to earlier stages (e.g., won't re-generate PRD)

## Notes

- Set `JIRA_WEBHOOK_SECRET=` (empty) in `.env` to skip signature validation during testing
- Change `TEST-123` to match your actual ticket key
- The workflow must be running (`uv run forge worker`)
