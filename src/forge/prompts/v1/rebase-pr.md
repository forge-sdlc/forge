You are resolving merge conflicts in a Git repository. A `git merge origin/main` was
attempted on the PR branch and produced conflicts.

## Ticket

**Key:** {ticket_key}

## Conflicted Files

{conflicted_files}

## PR Description (for context on what this branch does)

{pr_description}

## Files Changed by This Branch

{changed_files}

## Instructions

1. Read each conflicted file and understand both sides of the conflict
2. Use the PR description and changed files list to understand the intent of the branch's changes
3. Resolve each conflict by choosing the correct combination of both sides:
   - Preserve the branch's intentional changes (the feature/fix being implemented)
   - Incorporate any necessary updates from main (new APIs, renamed functions, moved code, etc.)
   - Do NOT simply accept one side — merge intelligently
4. Remove all conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`)
5. Ensure the resolved files are syntactically valid
6. Run any available linters or tests to verify the resolution
7. Stage all resolved files with `git add`
8. Do NOT create a commit — the orchestrator handles that
