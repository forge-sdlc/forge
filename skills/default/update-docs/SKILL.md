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

Use `FORGE_BASE_REF` supplied by the runtime. Resolve its merge base with HEAD,
then inspect the diff from that commit to the working tree, including staged and
unstaged edits. If the base cannot be resolved, report that limitation; do not
claim NO_DOCS_UPDATED. An actually empty diff needs no documentation changes.

### 2. Discover documentation files

Use tracked paths (`git ls-files`) to discover relevant Markdown, reStructuredText,
AsciiDoc, text documentation, and repository-specific documentation formats.
Include docs already modified in this PR: partial updates can still be stale.
Exclude generated content and historical release/changelog entries. Do not impose
an arbitrary first-N-files cutoff; record any genuine search limitation.

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

Batch literal identifier searches across the discovered documentation, using
`rg -F` with a pattern file or equivalent. Quote identifiers and handle filenames
safely. For example:

```bash
rg -n -F -f .forge/changed-identifiers.txt -- <document-paths>
```

Deduplicate identifiers before searching. Also inspect relevant guides for
changed behavior described without symbol names, such as defaults or retry rules.

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
in pass 1, read enough surrounding context alongside the relevant section of the
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

1. Reuse the context already inspected; read additional lines only as needed
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
