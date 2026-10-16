## Task Takeover Qualitative Review

Use the task-takeover-review skill. This is a read-only review: assess the assigned
acceptance criteria, relevant current source, and recorded validation evidence.
Use relevant repo-local review guidance within these stage permissions.

### Workspace
{workspace_path}

### Ticket Acceptance Criteria
{acceptance_criteria}

### Full Implementation Diff
{git_diff}

Return exactly one verdict on its own line:
`verdict: adequate` or `verdict: tests_incomplete`
Then `feedback: <specific findings, evidence, and validation limitations>`.

Use adequate when the assigned requirements are met and validation is appropriate
for the change. Behavior changes require meaningful regression coverage; docs-only
changes may use link/render/lint checks without a new test file. Use
tests_incomplete for unmet criteria or insufficient evidence, including an
unavailable diff. Never claim to have run tests during read-only review.
