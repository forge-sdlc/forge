---
max_retries: 3
---

# Implementation Review

Review the code changes for quality, completeness, and adherence to project standards. Run `git diff origin/main...HEAD` to see all changes on this branch.

## Review Criteria

Evaluate the diff against each criterion. Flag violations; do not flag items that pass.

### 1. Test Coverage

New public functions, methods, or exported symbols must have corresponding tests.

- Public function added without test? Flag it.
- Existing test file updated to cover new behavior? Passes.
- Internal/private helpers without direct tests are acceptable if exercised by public API tests.

### 2. Error Handling

Errors must be handled explicitly with specific exception types or error values.

- Bare `except:` (Python), empty `catch {}` (JS/TS), or `_ =>` discarding errors (Rust)? Flag it.
- Generic catches that log and re-raise, or catch-all with explicit handling? Acceptable.
- Go: Errors must be checked, not discarded with `_`.

### 3. Documentation

Public APIs must have documentation.

- Public function/method without docstring, JSDoc, GoDoc, or rustdoc? Flag it.
- Internal/private symbols without docs? Acceptable.
- Complex logic without inline comments explaining the "why"? Consider flagging.

### 4. No Debug Code

Committed code must not contain debug artifacts.

- `print()`, `console.log()`, `fmt.Println()` for debugging (not intentional logging)? Flag it.
- `TODO`, `FIXME`, `XXX`, `HACK` comments in new code? Flag it.
- Commented-out code blocks? Flag it.
- Intentional logging via proper logger frameworks? Acceptable.

## Verdict

After reviewing all criteria, output exactly one of these markers:

```
APPROVED
```

Use when all criteria pass or only minor issues exist that don't block merge.

```
REJECTED
```

Use when any criterion fails with blocking issues.

## Feedback Format

On rejection, provide structured feedback for each violation:

```
REJECTED

Issues:
- [Criterion: Test Coverage] file.py:42 — `process_data()` is public but has no test
- [Criterion: Error Handling] handler.go:87 — error from `db.Query()` discarded with `_`
- [Criterion: No Debug Code] utils.ts:15 — contains `console.log()` debug statement
- [Criterion: Documentation] api.rs:23 — public function `calculate_score` lacks rustdoc

Required changes:
1. Add test for `process_data()` in `test_file.py`
2. Handle or propagate error from `db.Query()` at line 87
3. Remove `console.log()` or replace with proper logger
4. Add documentation comment for `calculate_score`
```

On approval with optional suggestions:

```
APPROVED

Notes:
- Consider adding edge case tests for `validate_input()` (non-blocking)
```

## Important

- Be specific: include file path and line number for every issue
- Be actionable: state exactly what needs to change
- Be fair: don't flag acceptable patterns listed above
- Stay language-agnostic: apply criteria to any stack (Python, Go, Node.js, Rust, etc.)
