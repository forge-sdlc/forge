# Proposals

Forge uses a lightweight proposal process for significant changes. Proposals live in the [`proposals/`](https://github.com/forge-sdlc/forge/tree/main/proposals) directory of the repository.

## Creating a Proposal

1. Copy `proposals/TEMPLATE.md` to `proposals/NNN-feature-name.md`
2. Fill in all sections
3. Submit a PR for review and discussion

For smaller changes (bug fixes, default skill improvements), open a GitHub issue instead. Proposals are for changes that affect the core workflow, add new pipeline stages, or touch the system in ways that benefit from broad alignment first.

## Proposal Lifecycle

| Status | Meaning |
|--------|---------|
| **Draft** | Initial idea, not yet ready for review |
| **Under Review** | Open for feedback and discussion |
| **Accepted** | Approved for implementation |
| **Rejected** | Not moving forward (rationale documented) |
| **Implemented** | Feature built and merged |

## Index

| # | Title | Status |
|---|-------|--------|
| [001](https://github.com/forge-sdlc/forge/blob/main/proposals/001-qa-mode-for-generated-artifacts.md) | Q&A Mode for Generated Artifacts | Implemented |
| [002](https://github.com/forge-sdlc/forge/blob/main/proposals/002-workflow-status-updates-in-jira.md) | Workflow Status Updates in Jira | Implemented |
| [003](https://github.com/forge-sdlc/forge/blob/main/proposals/003-retryable-blocked-state.md) | Retryable Blocked State via `forge:retry` | Implemented |
| [004](https://github.com/forge-sdlc/forge/blob/main/proposals/004-dynamic-skill-loading.md) | Dynamic Skill Loading by Jira Project | Implemented |
| [005](https://github.com/forge-sdlc/forge/blob/main/proposals/005-ci-gate-skip-command.md) | CI Gate Skip via Comment Command | Implemented |
| [006](https://github.com/forge-sdlc/forge/blob/main/proposals/006-ci-fix-pr-description-sync.md) | PR Description Sync After CI Fix Commits | Implemented |
| [007](https://github.com/forge-sdlc/forge/blob/main/proposals/007-implement-review-node.md) | Dedicated `implement_review` Node for PR Review Feedback | Implemented |
| [008](https://github.com/forge-sdlc/forge/blob/main/proposals/008-stable-pr-to-ticket-association.md) | Stable PR-to-Ticket Association via State Lookup | Draft |
| [009](https://github.com/forge-sdlc/forge/blob/main/proposals/009-skill-installer.md) | Skill Packages via Jira Project Metadata | Draft |
| [010](https://github.com/forge-sdlc/forge/blob/main/proposals/010-project-metadata-repos.md) | Repository Configuration via Jira Project Metadata | Implemented |
