Update documentation files that have become stale due to code changes in a separate repository.

## Workspace

{workspace_path}

The documentation repository is mounted at `/workspace` (read-write). The code repository is mounted at `/code-repo` (read-only).

Run `cd /code-repo && git diff origin/{code_default_branch}...HEAD` to see the code changes, and check `/code-repo/.forge/handoff.md` for context on what was implemented and why.

Then follow the update-docs skill process to find and update any documentation files in `/workspace` whose content has become incorrect due to the code changes.

## Project Guidelines

{guardrails}
