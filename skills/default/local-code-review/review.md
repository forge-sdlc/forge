---
max_retries: 2
---

# Local Code Review Quality Gate

This review runs after the local-code-review skill completes. It validates the changes are ready for PR creation by checking for breaking changes, test failures, security issues, and spec alignment.

## Before Reviewing

Run the project's test suite to verify all tests pass locally.

### Detecting the Test Command

Check for a test command in the following order:

1. **pyproject.toml** — look for `[tool.pytest]` or `[project.scripts]` with test entry
   - Default: `uv run pytest` or `pytest`
2. **package.json** — look for `scripts.test`
   - Default: `npm test`
3. **Makefile** — look for `test:` target
   - Default: `make test`
4. **Cargo.toml** — Rust project
   - Default: `cargo test`
5. **go.mod** — Go project
   - Default: `go test ./...`

Run the detected test command. If tests fail, this review must be `REJECTED`.

## Review Criteria

Evaluate the diff (`git diff origin/main...HEAD`) against each criterion. Flag violations; do not flag items that pass.

### 1. Breaking Changes

API or interface changes must not break existing consumers without a migration path.

- Public function signature changed (parameters added/removed, types changed) without deprecation or migration? Flag it.
- Public class/struct fields removed or renamed without migration? Flag it.
- Exported constant or enum value removed? Flag it.
- Internal/private changes that don't affect the public API? Acceptable.
- Changes with documented migration in commit message or changelog? Acceptable.

### 2. Test Failures

All tests must pass locally before PR creation.

- Test suite executed and any test failed? Flag it with the failing test name(s).
- Test suite not executed (skipped or couldn't run)? Flag it — tests must run.
- All tests pass? Acceptable.

### 3. Security Issues

Code must not introduce security vulnerabilities.

- **Hardcoded secrets**: API keys, passwords, tokens, or credentials in source code? Flag it.
- **SQL injection**: String concatenation or f-strings in SQL queries instead of parameterized queries? Flag it.
- **Path traversal**: User input used directly in file paths without validation or sanitization? Flag it.
- **Shell injection**: User input passed to shell commands without proper escaping? Flag it.
- Environment variables, config files, or secret managers for credentials? Acceptable.
- Parameterized queries or ORM methods? Acceptable.
- Path validation with allowlists or canonicalization? Acceptable.

### 4. Spec Alignment

Changes must match the task description and acceptance criteria.

- Core requirement from the task description clearly not implemented? Flag it.
- Acceptance criterion explicitly inverted (does the opposite of what's specified)? Flag it.
- Implementation approach differs but satisfies the requirement? Acceptable.
- Additional functionality beyond spec (if not breaking anything)? Acceptable.

## Verdict

After reviewing all criteria, output exactly one of these markers:

```
APPROVED
```

Use when all criteria pass:
- No breaking changes without migration
- All tests pass locally
- No security vulnerabilities detected
- Implementation aligns with task description

```
REJECTED
```

Use when any criterion fails.

## Feedback Format

On rejection, provide structured feedback for each violation:

```
REJECTED

Issues:
- [Criterion: Test Failures] `test_user_auth` failed with AssertionError
- [Criterion: Breaking Changes] api.py:42 — `get_user()` parameter `user_id` changed to `id` without deprecation
- [Criterion: Security] db.py:87 — SQL query uses f-string: `f"SELECT * FROM users WHERE id = {user_id}"`
- [Criterion: Spec Alignment] handler.py:15 — task requires validation of email format but no validation implemented

Required changes:
1. Fix failing test `test_user_auth` or update test to match new behavior
2. Add `user_id` as deprecated alias for `id` parameter or document breaking change
3. Use parameterized query: `cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))`
4. Add email format validation using regex or email-validator library
```

On approval:

```
APPROVED

All criteria pass:
- No breaking API changes detected
- All 47 tests pass
- No security issues found
- Implementation matches task requirements
```

## Important

- Run tests first — this is a hard requirement, not optional
- Be specific: include file path and line number for every issue
- Be actionable: state exactly what needs to change
- Security issues are always blocking — never approve code with hardcoded secrets or injection vulnerabilities
- This is a pre-PR gate — catch issues before they reach CI or human reviewers
