# PR Commands

Forge listens for commands posted as comments on GitHub pull requests. Some commands are stage-specific (CI gates), while others work from any workflow stage.

## Available Commands

### `/forge skip-gate <name>`

Bypass a specific CI check. Use this for infrastructure failures unrelated to your code (cloud outages, quota exhaustion, flaky test runners).

```
/forge skip-gate <check-name-substring>
```

**Examples:**

```
/forge skip-gate e2e-openstack-ovn
/forge skip-gate e2e-openstack        ← skips all checks containing this substring
/forge skip-gate flaky-integration
```

**What Forge does:**

1. Replies on the PR confirming the skip with the matched check name
2. Posts an audit comment on the Jira ticket
3. Re-evaluates CI treating the skipped check as passing

**Persistence:** Skips persist across pushes. If the same infrastructure check fails again after the next commit, it is still treated as passing.

**Matching:** Case-insensitive substring match against the full check name.

---

### `/forge unskip-gate <name>`

Remove a previously set skip.

```
/forge unskip-gate e2e-openstack-ovn
```

Forge confirms the removal and re-evaluates CI without the skip.

---

### `/forge rebase`

Merge `main` into the PR branch and resolve any merge conflicts using AI. Use this when the PR falls behind `main` and develops conflicts — especially important for fork-based PRs where GitHub won't run CI on conflicted branches.

```
/forge rebase
```

**What Forge does:**

1. Replies on the PR confirming the rebase was triggered
2. Posts an audit comment on the Jira ticket
3. Clones the repository and checks out the PR branch from the fork
4. Attempts `git merge origin/main`
5. If no conflicts: pushes the merge commit to the fork branch
6. If conflicts: spawns a container with Claude to resolve them using the PR description and changed files as context
7. Verifies no conflict markers remain, commits, and force-pushes to the fork
8. Returns the workflow to the node it was at before the rebase

**When it works:** Unlike skip-gate commands, `/forge rebase` works from **any workflow stage** — CI gate, human review, implementation, or any other point where a PR exists.

**Conflict resolution:** The AI agent reads each conflicted file, understands the intent of both the branch changes and the incoming main changes, and merges them intelligently. It preserves the branch's intentional changes while incorporating necessary updates from main (new APIs, renamed functions, moved code, etc.).

**If resolution fails:** Forge aborts the merge, posts a comment explaining the failure, and returns the workflow to its previous state. Manual intervention is needed.

## When Commands Are Active

**Skip-gate and unskip-gate** only work when Forge's workflow is in a CI stage:

- `wait_for_ci_gate`
- `ci_evaluator`
- `attempt_ci_fix`

**Rebase** works from any workflow stage where a PR exists in the workflow state.

## Permanently Ignored Checks

Some checks are always pending and are permanently ignored regardless of skip commands. Configure the list with `CI_IGNORED_CHECKS` in `.env`.

Common examples: `tide` (Prow's merge-queue controller), status checks that reflect queue position rather than test results.

## Audit Trail

Every skip and unskip action is recorded as a comment on the Jira ticket, so there's a clear record of which checks were bypassed and when.
