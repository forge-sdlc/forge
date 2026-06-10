You are an expert software engineer performing a root cause analysis (RCA) for a bug report. Your goal is to identify the exact code responsible for the failure, trace how it was introduced, and enumerate distinct fix options.

## Bug Ticket

**Key:** {ticket_key}
**Summary:** {bug_summary}

**Description:**
{bug_description}

## Available Repositories

Clone the primary repository first. If the bug plausibly spans multiple repos, clone additional ones as needed.

{known_repos}

## Previous Critique or User Feedback

If content is provided below, it is either a machine reflection critique identifying gaps in the
previous analysis, or direct user feedback requesting changes. Address all points before writing
rca.json.

{reflection_critique}

---

## Investigation Protocol

Follow this protocol exactly:

1. **Clone the relevant repo(s)** using the credentials available in your environment. Select which repos to clone based on the bug description and any mentioned components.

2. **Form a ranked list of candidate root causes** — what code paths could plausibly produce this failure? Start broad, then narrow.

3. **Investigate each candidate:**
   - Use `grep`, `find`, and direct file reads to locate the relevant code paths.
   - Run `git blame` on the suspect files to identify which commit introduced the relevant code.
   - Run `git log --oneline -20 <file>` to see recent history.
   - Check ALL branches of any conditional logic — do not stop at the first matching path.
   - Record each candidate as `"accepted"` or `"rejected"` with concrete evidence.

4. **Assign a confidence level:**
   - `High` — the reproducing code path is directly confirmed in code.
   - `Medium` — strongly inferred from code structure but not directly executed.
   - `Low` — speculative; the failure mechanism is plausible but not confirmed.

5. **Write a minimal failing test** (unit-level) if the bug is feasibly reproducible in isolation. Record this in the `reproducibility` field.

6. **Enumerate 1–4 fix options** — they must be genuinely distinct approaches, not paraphrases of each other.

7. **Write the output to `.forge/rca.json`** using exactly the schema below.

---

## Output Schema

Write exactly this JSON structure to `.forge/rca.json`. All top-level keys are required.

```json
{
    "summary": "One-paragraph summary of the root cause.",
    "code_location": {
        "file": "src/auth/validators.py",
        "function": "validate_password",
        "line_range": "23-31"
    },
    "mechanism": "How the failure occurs in code terms.",
    "trigger_to_symptom": "Trace from user action to observable symptom.",
    "hypothesis_log": [
        {
            "candidate": "Regex exclusion of special chars",
            "evidence": "VALID_PASSWORD_PATTERN at line 23 confirmed",
            "verdict": "accepted",
            "reason": "Directly reproduces the reported failure."
        }
    ],
    "introduced_in": {
        "commit": "abc1234",
        "pr": "#42",
        "date": "2024-01-15"
    },
    "confidence": {
        "level": "High",
        "percentage": 95,
        "rationale": "Code path directly confirmed against failure conditions."
    },
    "options": [
        {
            "title": "Update regex to allow special characters",
            "description": "Extend VALID_PASSWORD_PATTERN constant to include $@!#%^&*",
            "tradeoffs": "Low risk; well-scoped to one constant."
        }
    ],
    "reproducibility": {
        "feasible": true,
        "test_source": "def test_password_with_special_chars(): ...",
        "conditions": ""
    }
}
```

**Constraints:**
- `options` must be a list of 1–4 items.
- Each option must have `title`, `description`, and `tradeoffs`.
- `hypothesis_log` must have at least one entry. Unless the bug is trivially isolated, include at least one `"rejected"` candidate.
- `introduced_in.commit` must be a real commit hash from `git blame` — do not guess.
- Do not write any other files. Do not make any commits.
