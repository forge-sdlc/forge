"""LLM judge for evaluating Forge skill outputs against gold standards."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import yaml


def _create_client():
    vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    vertex_region = os.environ.get("ANTHROPIC_VERTEX_REGION", "us-east5")
    if vertex_project:
        return anthropic.AnthropicVertex(project_id=vertex_project, region=vertex_region)
    return anthropic.Anthropic()


@dataclass
class CriterionResult:
    id: str
    name: str
    weight: str
    passed: bool
    score: int
    reasoning: str
    quotes: list[str]


@dataclass
class EvalReport:
    skill: str
    generated_path: str
    gold_path: str
    results: list[CriterionResult]
    pass_threshold: int = 6
    fail_on_critical: bool = True

    @property
    def total_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total_criteria(self) -> int:
        return len(self.results)

    @property
    def total_score(self) -> int:
        return sum(r.score for r in self.results)

    @property
    def max_score(self) -> int:
        return len(self.results) * 2

    @property
    def critical_failures(self) -> list[CriterionResult]:
        return [r for r in self.results if r.weight == "critical" and not r.passed]

    @property
    def overall_pass(self) -> bool:
        if self.fail_on_critical and self.critical_failures:
            return False
        return self.total_passed >= self.pass_threshold

    @property
    def grade(self) -> str:
        pct = self.total_score / self.max_score if self.max_score else 0
        if pct >= 0.9 and not self.critical_failures:
            return "A"
        if pct >= 0.75 and not self.critical_failures:
            return "B"
        if pct >= 0.6:
            return "C"
        return "D"


def load_criteria(criteria_path: Path) -> dict:
    with open(criteria_path) as f:
        return yaml.safe_load(f)


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text that may contain preamble."""
    if text.startswith("```"):
        parts = text.split("\n", 1)
        if len(parts) == 2:
            text = parts[1].rsplit("```", 1)[0]

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{[^{}]*"pass"\s*:', text)
    if match:
        start = match.start()
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    return None


def judge_criterion(
    client,
    model: str,
    criterion: dict,
    generated: str,
    gold: str,
) -> CriterionResult:
    system = (
        "You are a document quality judge. Score the generated document against "
        "the gold standard for the specific criterion described.\n\n"
        "CRITICAL: Return ONLY a JSON object. No thinking, no analysis, no preamble.\n"
        "Do NOT explain your reasoning before the JSON. Start your response with {.\n\n"
        "JSON schema:\n"
        '{"pass": true/false, "score": 0-2, "reasoning": "one sentence", "quotes": ["relevant quote"]}\n\n'
        "Scoring: 0 = fails the criterion, 1 = partially meets, 2 = fully meets.\n"
        "Keep reasoning to one sentence. Keep quotes short (under 100 chars each, max 3)."
    )

    user = (
        f"## Criterion: {criterion['name']}\n"
        f"{criterion['prompt']}\n\n"
        f"## Generated Document\n```\n{generated}\n```\n\n"
        f"## Gold Standard Document\n```\n{gold}\n```"
    )

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    text = response.content[0].text.strip()
    data = _extract_json(text)

    if data is None:
        return CriterionResult(
            id=criterion["id"],
            name=criterion["name"],
            weight=criterion.get("weight", "important"),
            passed=False,
            score=0,
            reasoning=f"Judge returned unparseable response: {text[:200]}",
            quotes=[],
        )

    return CriterionResult(
        id=criterion["id"],
        name=criterion["name"],
        weight=criterion.get("weight", "important"),
        passed=data.get("pass", False),
        score=data.get("score", 0),
        reasoning=data.get("reasoning", ""),
        quotes=data.get("quotes", []),
    )


def evaluate(
    criteria_path: Path,
    generated_path: Path,
    gold_path: Path,
) -> EvalReport:
    config = load_criteria(criteria_path)
    model = config.get("judge_model", "claude-sonnet-4-6")

    generated = generated_path.read_text()
    gold = gold_path.read_text()

    client = _create_client()
    results = []

    for criterion in config.get("criteria", []):
        print(f"  Judging: {criterion['name']}...", end="", flush=True)
        result = judge_criterion(client, model, criterion, generated, gold)
        status = "PASS" if result.passed else "FAIL"
        print(f" {status} {result.score}/2")
        results.append(result)

    return EvalReport(
        skill=config.get("skill", "unknown"),
        generated_path=str(generated_path),
        gold_path=str(gold_path),
        results=results,
        pass_threshold=config.get("pass_threshold", 6),
        fail_on_critical=config.get("fail_on_critical", True),
    )
