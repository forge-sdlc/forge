Implement the assigned changes in the workspace. Before committing, record
validation in .forge/validation.md: acceptance criterion, command/check, tested
revision or working-tree state, exit code/result, and unavailable checks with
reasons. Update .forge/handoff.md even if blocked; report partial work honestly.

Stage only specific implementation files, review git diff --cached, and commit
with a descriptive message referencing the task key. Never commit .forge/ or
credentials. Do not push; orchestration owns remote actions. Do not change
.gitignore merely to hide Forge artifacts. An explicitly requested .gitignore
change is permitted. Do not create an empty commit when no change is needed.
