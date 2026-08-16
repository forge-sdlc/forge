# Skill Evaluation

Evaluate Forge skill outputs against gold standards using an LLM judge.

## How It Works

The evaluator sends each criterion to an LLM judge (Sonnet by default) with
the generated artifact and a gold standard. The judge returns a structured
score (0-2) with reasoning and evidence quotes.

## Criteria Files

Criteria are defined per skill in YAML at
`devtools/test-skill/evaluators/criteria/{skill-name}.yaml`:

```yaml
skill: generate-prd
description: "PRD quality criteria"
judge_model: claude-sonnet-4-6
gold_standard_file: gold-prd.md

generated_file_patterns:
  - "enhancements/*/prd.md"

criteria:
  - id: scope-accuracy
    name: "Scope Accuracy"
    weight: critical
    prompt: |
      Compare In Scope and Out of Scope items against the gold standard.
      Flag items in the wrong section or missing items.

  - id: template-compliance
    name: "Template Compliance"
    weight: important
    prompt: |
      Check that the document follows the required template sections.

pass_threshold: 6
fail_on_critical: true
```

### Fields

| Field | Description |
|-------|-------------|
| `skill` | Skill name for labeling |
| `judge_model` | LLM model for judging (default: `claude-sonnet-4-6`) |
| `gold_standard_file` | Expected gold file name in each dataset case directory |
| `generated_file_patterns` | Glob patterns to find the generated artifact in output |
| `criteria` | List of evaluation criteria (see below) |
| `pass_threshold` | Minimum criteria that must pass for overall PASS |
| `fail_on_critical` | If `true`, any critical failure causes overall FAIL |

### Criterion Fields

| Field | Description |
|-------|-------------|
| `id` | Short identifier (used in JSON reports and MLflow metrics) |
| `name` | Human-readable name |
| `weight` | `critical` or `important` |
| `prompt` | Instructions for the judge — what to check and how |

### Weights

| Weight | Effect |
|--------|--------|
| `critical` | When `fail_on_critical: true`, any critical failure causes overall FAIL |
| `important` | Counts toward `pass_threshold` but doesn't auto-fail |

### Scoring

| Score | Meaning |
|-------|---------|
| 0 | Fails the criterion |
| 1 | Partially meets |
| 2 | Fully meets |

### Grades

| Grade | Condition |
|-------|-----------|
| A | Score >= 90% and no critical failures |
| B | Score >= 75% and no critical failures |
| C | Score >= 60% |
| D | Below 60% |

## Running Evaluations

### Single artifact

```bash
forge test-skill eval \
  --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
  --generated output/prd.md \
  --gold gold-prd.md \
  --output output/eval/
```

### Batch (full dataset)

```bash
forge test-skill eval \
  --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
  --dataset eval/dataset/cases/ \
  --results-dir output/ \
  --output output/eval/
```

Dataset structure:

```
eval/dataset/cases/
├── PROJ-1234/
│   ├── input.yaml
│   └── gold-prd.md
├── PROJ-5678/
│   ├── input.yaml
│   └── gold-prd.md
```

## Reports

Three output formats per evaluation:

| Format | File | Use |
|--------|------|-----|
| Terminal | stdout | Quick pass/fail check |
| JSON | `results.json` | CI integration, programmatic analysis |
| HTML | `report.html` | Visual review with evidence quotes |

## Writing Good Criteria

- One specific thing per criterion — don't combine multiple checks
- Include concrete examples of what to look for in the prompt
- Use `critical` weight sparingly — only for requirements that indicate
  a fundamentally wrong output
- The judge sees both generated and gold documents — phrase prompts as
  comparisons ("Compare X against the gold standard")
- Keep prompts focused: vague criteria produce inconsistent scores
