---
name: update-docs
description: >-
  Detects documentation files that have become stale due to code changes
  and applies minimal targeted updates. Builds an identifier checklist
  from the diff, greps documentation for matches, evaluates candidates
  in two passes, and edits confirmed stale docs in-place.
---

# Update Docs

Code changes can silently invalidate documentation. A renamed function,
a changed API signature, a removed configuration option — each can leave
docs describing behavior that no longer exists. This skill detects that
drift by matching the code diff against in-repo documentation and
updating docs whose descriptions contradict the new code.

## Process

Follow these steps in order. Do not skip steps.

### 1. Get the diff

```bash
DEFAULT_BRANCH=$(git rev-parse --abbrev-ref origin/HEAD | cut -d/ -f2)
git diff $(git merge-base HEAD "$DEFAULT_BRANCH")..HEAD --no-color
```

Record the list of files changed in the PR:

```bash
git diff --name-only $(git merge-base HEAD "$DEFAULT_BRANCH")..HEAD
```

If the diff is empty, output `NO_DOCS_UPDATED` and stop.

### 2. Discover documentation files

Explore the repository structure to identify where documentation lives.
Different repos organize docs differently — look for dedicated doc
directories (`docs/`, `doc/`, `documentation/`), standalone files like
`README.md` at any level, and any other files whose primary purpose is
documentation.

Search broadly:

```bash
find . -type f \( -name "*.md" -o -name "*.rst" -o -name "*.adoc" -o -name "*.txt" \) \
  ! -path "./.git/*" ! -path "./.forge/*" ! -path "./vendor/*" ! -path "./node_modules/*" \
  | head -500
```

Then filter the results:

- **Exclude** files already modified in the PR (they are being actively
  updated — check against the changed file list from step 1)
- **Exclude** auto-generated files (lockfiles, generated API docs,
  swagger output)
- **Exclude** changelog and release note entries that describe past
  releases

If no documentation files exist in the repo, output `NO_DOCS_FOUND`
and stop.

### 3. Build the identifier checklist

Go through **every** changed file in the PR. For each file, extract
identifiers from the modified lines (lines starting with `+` or `-`)
and from diff hunk headers (`@@` lines). Write them down as a numbered
checklist — one entry per changed file, with all identifiers from that
file.

Use the most specific form of each identifier. CLI flag names,
configuration keys, full function names, and type names are good —
they match only relevant docs. Avoid generic short words that would
match hundreds of unrelated files. If a generic term is the only
identifier available for a change, include it, but prefer specific
forms when they exist.

Do not skip files. Do not prioritize some files over others. Every
changed file gets an entry in the checklist.

### 4. Search docs for every identifier

Write a shell script that takes the identifiers from step 3 and
greps for each one across the documentation files from step 2.
Run the script in a single Bash call:

```bash
for id in "identifier1" "identifier2" "identifier3"; do
  matches=$(grep -rl "$id" <doc_files> 2>/dev/null)
  if [ -n "$matches" ]; then
    echo "MATCH: $id -> $matches"
  fi
done
```

Include every identifier from every checklist entry in the `for`
loop. The script handles the searching mechanically — no identifiers
are skipped.

From the script output, collect all matched doc files into a
candidate list.

### 5. Evaluate every candidate (two passes)

**Pass 1 — Quick scan.** For each candidate doc file from step 4,
view only the lines that matched the grep (use `grep -n` to see them
in context). Based on the matching lines alone, decide whether the
doc might be stale. Record a verdict for every candidate:

```
- path/to/doc.md -> possibly stale (describes behavior that changed)
- path/to/other.md -> not stale (mentions identifier in passing)
- path/to/another.md -> not stale (changelog entry)
...
```

Every candidate must have a verdict. Do not skip candidates.

**Pass 2 — Deep read.** For each candidate marked "possibly stale"
in pass 1, read the full file alongside the relevant section of the
diff. Confirm whether the doc is actually stale.

When evaluating:

- **Only flag docs whose content is now incorrect.** A doc that
  mentions an identifier is not stale if the described behavior is
  unchanged. It is stale only if the behavior, signature, or semantics
  changed in a way that makes the doc misleading.
- **Do not flag changelog entries or release notes that describe past
  releases.** Historical entries are not stale because the code evolved.

### 6. Update confirmed stale docs

For each doc confirmed stale in pass 2:

1. Read the full file
2. Make minimal targeted edits — fix only what the diff invalidated
3. Do NOT restructure, rewrite, or add content beyond what the code
   change requires
4. Preserve the file's existing format, style, and structure

Update the documentation so it accurately reflects the new code.

### 7. Commit changes

If any documentation files were updated:

```bash
git add <updated_doc_files>
git commit -m "[TICKET_KEY] docs: update documentation for code changes"
```

Replace TICKET_KEY with the actual ticket key from the task context.

### 8. Output

If docs were updated:
```
DOCS_UPDATED

Updated:
- [path/to/doc.md] Brief description of what was updated and why
- [path/to/other.rst] Brief description
```

If no docs needed updating:
```
NO_DOCS_UPDATED
```

If no doc files found:
```
NO_DOCS_FOUND
```

## Constraints

- **Only change what the diff affects.** Do not improve, restructure,
  or reformat documentation that is unrelated to the code change.
- **Do not update historical entries.** Changelog and release note
  entries for past releases are not stale — they describe what happened
  at that point in time.
- **Update existing files only.** Do not create new doc files from
  scratch.
- **Preserve format.** Match the existing file's formatting conventions
  (heading style, list style, code block syntax, etc.).
