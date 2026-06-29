---
name: implement-task
description: Implement code changes according to Task specifications. Use when executing implementation Tasks.
---

# Task Implementation Skill

Implement code changes following Task specifications and project standards.

## Instructions

1. Read and understand the Task description
2. Review acceptance criteria carefully
3. Plan minimal, focused changes
4. Implement following project patterns
5. Document non-obvious decisions

## Implementation Rules

1. **Minimal Changes**: Only modify what's necessary for the Task
2. **Follow Patterns**: Match existing code style and architecture
3. **Test Coverage**: Include tests for new functionality
4. **Clean Code**: Self-documenting, well-structured code
5. **No Scope Creep**: Don't fix unrelated issues
6. **Focused investigation**: Start from the files, nearby code patterns, and tests identified by the Task. Broaden investigation as needed to implement safely and satisfy acceptance criteria.
7. **Ordering invariants**: Before committing, read the plan's `## Ordering Invariants` section. For each entry, locate the relevant calls in your implementation and confirm the stated order is preserved. If the section says "None identified.", skip this step.

## File Change Format

When providing code changes, format as:

```path/to/file.py
<complete file contents>
```

## Output Format

```markdown
## Summary
[Brief description of changes to be made]

## Files Changed

### path/to/file1.py
[Explanation of changes]

### path/to/file2.py
[Explanation of changes]

## Implementation Notes
[Any important decisions or considerations]

## Code Changes

[File blocks with complete contents]
```

## Quality Checklist

Before submitting implementation:

- [ ] All acceptance criteria addressed
- [ ] Tests included for new functionality
- [ ] No unrelated changes
- [ ] Code follows project conventions
- [ ] Error handling appropriate
- [ ] No hardcoded values that should be configurable
- [ ] Ordering invariants verified: each entry in `## Ordering Invariants` is preserved in the implementation (skip if section says "None identified.")
