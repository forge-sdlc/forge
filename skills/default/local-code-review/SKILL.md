---
name: local-code-review
description: Run codegen, lint, then review local code changes for breaking issues and fix them in-place. Use before PR creation to catch critical problems early.
---

# Local Code Review Skill

Before creating a PR, ensure the code is clean and correct. This skill runs in three phases: codegen, lint/format, then a review for breaking issues.

## Phase 1 — Codegen

Run any required code generation so that generated files are in sync with source changes.

1. Check for codegen instructions in `README.md`, `CONTRIBUTING.md`, or `Makefile`
2. Check for `//go:generate` directives in files that were changed (`git diff --name-only "$(git merge-base HEAD "$FORGE_BASE_REF")" --`)
3. Run the appropriate codegen command (e.g. `go generate ./...`, `make generate`, `controller-gen`)
4. If codegen produces changes, they will be included in the commit

## Phase 2 — Lint & Format

Run the project's linter and formatter on the changed files.

1. Check for lint/format commands in `README.md`, `CONTRIBUTING.md`, or `Makefile`
2. If documented, use those commands. If not, fall back to language defaults:

| Language | Format | Lint |
|----------|--------|------|
| Go | `gofmt -w <file>` | `go vet ./pkg/changed/...` |
| Python | `ruff format <file>` | `ruff check --fix <file>` |
| TypeScript/JS | `prettier --write <file>` | `eslint --fix <file>` |
| Rust | `rustfmt <file>` | `cargo clippy -p crate_name` |

Run the formatter first, then the linter, targeting only the changed files.

## Phase 3 — Review

Get the diff and review for breaking issues only.

1. Inspect `git status --short` for newly generated/untracked files as well as the diff. Run `git diff "$(git merge-base HEAD "$FORGE_BASE_REF")" --no-color --` to get all changes on this branch
2. If the diff is empty, output `NO_BREAKING_ISSUES` and stop
3. For each breaking issue found:
   a. Identify the exact file and location
   b. Read the current file content using `read_file`
   c. Apply a targeted fix using `edit_file`
   d. Verify the fix compiles / passes a targeted test if possible
4. If no breaking issues are found, output `NO_BREAKING_ISSUES` and stop
5. After fixing, output a summary of what was fixed

## Scope — What to Review (Phase 3)

Only flag and fix issues that would cause:
- **Build or compile failures** — code that won't compile or import correctly
- **Runtime crashes** — null pointer dereferences, unhandled exceptions on the happy path, type errors
- **Security holes** — hardcoded secrets, SQL injection, shell injection, arbitrary code execution
- **Broken tests** — test failures introduced by the changes
- **Spec violations** — core acceptance criteria from the spec that are clearly unimplemented or inverted

Do NOT flag: style, formatting, naming, minor improvements, performance, missing docs, or anything already handled in Phase 2.

## Output Format

If no breaking issues:
```
NO_BREAKING_ISSUES
```

If breaking issues were found and fixed:
```
BREAKING_ISSUES_FIXED

Fixed:
- [file:line] Brief description of issue and fix applied
- [file:line] ...

Unfixed (could not resolve):
- [file:line] Brief description of why it couldn't be fixed
```

## Important

- Fix directly in the workspace files — do NOT post GitHub comments
- Be surgical: change only what is broken, nothing more
- If you cannot safely fix an issue, document it in the Unfixed section rather than making a risky change
