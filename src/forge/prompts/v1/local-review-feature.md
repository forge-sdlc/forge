Review the code changes on this branch for breaking issues. Do not modify files.

## Workspace

{workspace_path}

## Specification

{spec_content}

## Project Guidelines

{guardrails}

Run `git diff origin/main...HEAD` to understand what changed. You may run tests, lint, and read files, but you must not edit, create, delete, stage, or commit files.

Output your verdict as one of:
- `verdict: adequate`
- `verdict: tests_incomplete`

Followed by `feedback: <specific explanation>`.

Use `adequate` only if the implementation appears correct and relevant tests/lint pass or are reasonably covered. Use `tests_incomplete` if tests/lint fail, coverage is missing, behavior is incomplete, or the implementation needs changes.
