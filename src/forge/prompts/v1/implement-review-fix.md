Implement the code changes described in `.forge/review-plan.md`, then ensure the project builds cleanly.

## Instructions

1. Read `.forge/review-plan.md`. If it says `# No actionable items`, exit without changes.

2. Implement each item — minimal, targeted edits only.

3. After making changes, run the project's standard post-change steps (codegen, lint, build). Check `README.md`, `CONTRIBUTING.md`, `Makefile`, or `CLAUDE.md` for the correct commands. A typical sequence:
   - Regenerate any auto-generated files if source templates changed
   - Run the linter/formatter on changed files
   - Verify the build compiles

4. Amend the existing HEAD commit so review fixes stay on the original commit
   (preserves the original message and avoids a second commit that may fail
   per-commit CI checks):
   ```
   git add -A
   git commit --amend --no-edit
   ```
   Do **not** create a new commit with a different message. Only create a new
   commit if `git commit --amend` is impossible (for example, HEAD has no
   commits yet); in that rare case keep the original project commit style.

5. Do NOT push — the orchestrator handles that (force-push after amend).

Ticket: {ticket_key}
