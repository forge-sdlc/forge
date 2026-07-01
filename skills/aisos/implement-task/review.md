---
max_retries: 2
---

# Implementation Review — Forge/AISOS Override

This is a project-specific override for the Forge codebase. It inherits from the default review criteria in `skills/default/implement-task/review.md` and adds Forge-specific requirements.

> **For skill authors**: This file demonstrates per-project override patterns. Copy this structure to create overrides for your own project by placing it in `skills/{project-key-lowercase}/implement-task/review.md`.

## Base Criteria (Inherited)

Apply all criteria from the default `review.md`:

1. **Test Coverage** — Public functions must have tests
2. **Error Handling** — Explicit exception handling, no bare `except:`
3. **Documentation** — Public APIs need docstrings
4. **No Debug Code** — No stray `print()`, TODOs, or commented-out code

## Forge-Specific Criteria

These project-specific criteria extend the defaults for the Forge/AISOS codebase.

### 5. Testing with pytest

All tests must use pytest, not unittest or generic "run tests" approaches.

- Test files must be in `tests/` and match `test_*.py` naming
- Use pytest fixtures for setup, not `setUp()` methods
- Use `pytest.raises()` for exception testing, not try/except in tests
- Parameterized tests use `@pytest.mark.parametrize`, not loops
- Async tests use `pytest-asyncio` with `@pytest.mark.asyncio`

**Flag if**: `unittest.TestCase` subclasses, `self.assertEqual()` patterns, or missing pytest markers for async tests.

### 6. Type Hints on Public Functions

All public functions and methods must have type annotations.

- Return types are required (use `-> None` for procedures)
- Use `X | None` instead of `Optional[X]` (PEP 604)
- Use `list[T]` and `dict[K, V]` instead of `List` and `Dict` from typing
- Complex types should use `TypeAlias` or `TypedDict` for clarity

**Flag if**: Public function missing parameter types or return type annotation.

**Acceptable**: Private functions (prefixed with `_`) without full annotations.

### 7. Logging via logging Module

Use the `logging` module for all diagnostic output. Never use `print()` for logging.

- Import pattern: `logger = logging.getLogger(__name__)`
- Use appropriate log levels: `debug`, `info`, `warning`, `error`, `exception`
- Exception logging must use `logger.exception()` or pass `exc_info=True`
- Log messages should be lazy-formatted: `logger.info("Processing %s", item)` not f-strings

**Flag if**: `print()` calls for diagnostic output, f-string log messages, exceptions logged without stack traces.

**Acceptable**: `print()` in CLI entrypoints for user-facing output, test assertions.

### 8. Async/Await Patterns

Code using asyncio must follow proper async patterns.

- Async functions must be awaited at call sites
- Use `asyncio.gather()` for concurrent operations, not sequential awaits in loops
- Context managers for async resources use `async with`
- No blocking I/O (`time.sleep()`, sync HTTP calls) inside async functions
- Use `contextlib.suppress()` instead of try/except/pass patterns

**Flag if**: 
- `await` inside a for-loop when operations are independent (should use `gather()`)
- Sync `sleep()` or blocking calls in async context
- Missing `await` on coroutine calls
- `asyncio.run()` called from within an async context

**Acceptable**: Sequential awaits when operations have dependencies.

## Verdict

After reviewing all criteria (base + Forge-specific), output exactly one marker:

```
APPROVED
```

Use when all criteria pass or only minor issues exist that don't block merge.

```
REJECTED
```

Use when any criterion fails with blocking issues.

## Feedback Format

On rejection, include the criterion number and name for Forge-specific violations:

```
REJECTED

Issues:
- [Criterion: Test Coverage] src/forge/api/handler.py:42 — `process_webhook()` is public but has no test
- [Criterion: Type Hints] src/forge/models/task.py:15 — `parse_task()` missing return type annotation
- [Criterion: Logging] src/forge/worker.py:87 — uses `print()` for error output instead of logger
- [Criterion: Async/Await] src/forge/queue/consumer.py:32 — sequential awaits in loop should use `asyncio.gather()`

Required changes:
1. Add test for `process_webhook()` in `tests/unit/test_handler.py`
2. Add `-> Task` return type to `parse_task()` function signature
3. Replace `print(f"Error: {e}")` with `logger.error("Error: %s", e)`
4. Refactor loop to: `await asyncio.gather(*[process(item) for item in items])`
```

## Override Configuration

This file demonstrates per-project customization:

| Setting | Default | This Override | Rationale |
|---------|---------|---------------|-----------|
| `max_retries` | 3 | 2 | Forge CI is fast; 2 retries is sufficient |
| Test framework | generic | pytest | Forge uses pytest exclusively |
| Type hints | optional | required | Forge enforces mypy strict mode |
| Logging | generic | logging module | Forge uses structured logging |
| Async patterns | n/a | strict | Forge uses asyncio throughout |
