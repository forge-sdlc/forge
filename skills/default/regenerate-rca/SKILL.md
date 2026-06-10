---
name: regenerate-rca
description: Re-perform a root cause analysis after receiving feedback from the engineering team. Use when the team has reviewed an RCA and requested changes.
---

# RCA Regeneration Skill

You have previously investigated this bug and produced an RCA. The engineering team has reviewed it and provided feedback. Your job is to address every point in the feedback, then write an updated RCA.

## Instructions

1. **Re-examine the code** in light of the feedback. If the feedback identifies a missed hypothesis, investigate it. If it flags a wrong file location, find the correct one. If it questions your confidence level, gather more evidence.

2. **Do not discard valid findings** from your original RCA that the feedback did not challenge. Carry those forward.

3. **Update `introduced_in`** if the feedback suggests the commit attribution was wrong or uncertain. Use `git blame` and `git log` to confirm.

4. **Revise your fix options** if the feedback identifies missing alternatives or options that are too similar.

5. **Write the updated output to `.forge/rca.json`** using exactly the same schema as the original. All top-level keys are required.

The schema:
```json
{
    "summary": "...",
    "code_location": {"file": "...", "function": "...", "line_range": "..."},
    "mechanism": "...",
    "trigger_to_symptom": "...",
    "hypothesis_log": [{"candidate": "...", "evidence": "...", "verdict": "accepted|rejected", "reason": "..."}],
    "introduced_in": {"commit": "...", "pr": "...", "date": "..."},
    "confidence": {"level": "High|Medium|Low", "percentage": 0, "rationale": "..."},
    "options": [{"title": "...", "description": "...", "tradeoffs": "..."}],
    "reproducibility": {"feasible": true, "test_source": "...", "conditions": "..."}
}
```

Do not write any other files. Do not make any commits.
