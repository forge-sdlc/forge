You are analyzing CI failures to determine whether they are caused by changes
in this pull request or by external factors.

## CI Failure Information

The failed checks are described in:
{failures_file_path}

All available log files are in `.forge/logs/`.

## Your Task

Read the failure logs carefully. Compare the failing test names, file paths,
and error messages against the files changed in this PR.

To see every file this PR touches, compare the branch with the merge base of
the repository's default branch (`{base_branch}`):
```
git diff --name-only "$(git merge-base HEAD origin/{base_branch})" HEAD
```

Do not use only `HEAD~1..HEAD` or `git show HEAD`: those commands omit changes
from earlier commits in a multi-commit pull request.

Determine whether each failing check is caused by changes introduced in this PR,
or by external factors such as:
- Flaky or intermittently failing tests unrelated to the diff
- Broken test infrastructure (container images, network, environment config)
- Pre-existing failures in files not touched by this PR
- Tests that fail because of unrelated upstream changes

Write your verdict to `.forge/ci-attribution.json` in exactly this format:

```json
{
  "attributable": true,
  "reason": "one sentence explaining why",
  "confidence": "high"
}
```

Set `attributable` to `true` if the failure logs reference files, functions,
or test cases that appear in the PR diff. Set it to `false` if the failures
are clearly unrelated to the diff.

When confidence is low, set `attributable` to `true` (fail-safe: the fix
pipeline will attempt a fix, and the human can always use `/forge skip-gate`
to bypass a stuck check).

Do not attempt to fix anything. Write only the attribution JSON file.
