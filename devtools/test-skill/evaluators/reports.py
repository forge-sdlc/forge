"""Report generators for evaluation results."""

import html as html_mod
import json
from pathlib import Path

from .judge import EvalReport


def print_terminal(report: EvalReport):
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    print(f"\n{BOLD}PRD Evaluation: {report.skill}{RESET}")
    print("=" * 55)

    for r in report.results:
        color = GREEN if r.passed else RED
        status = "PASS" if r.passed else "FAIL"
        weight_marker = " *" if r.weight == "critical" else ""
        reasoning_short = r.reasoning[:60] if r.reasoning else ""
        print(f"  {r.name:<28} {color}{status}{RESET}  {r.score}/2  {reasoning_short}{weight_marker}")

    print("=" * 55)
    overall = f"{GREEN}PASS{RESET}" if report.overall_pass else f"{RED}FAIL{RESET}"
    grade_colors = {"A": GREEN, "B": GREEN, "C": YELLOW, "D": RED}
    gc = grade_colors.get(report.grade, RESET)
    print(
        f"  Total: {report.total_passed}/{report.total_criteria} passed | "
        f"Score: {report.total_score}/{report.max_score} | "
        f"Grade: {gc}{report.grade}{RESET} | {overall}"
    )

    if report.critical_failures:
        print(f"\n  {RED}Critical failures:{RESET}")
        for r in report.critical_failures:
            print(f"    - {r.name}: {r.reasoning[:80]}")

    print()


def save_json(report: EvalReport, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "skill": report.skill,
        "generated_path": report.generated_path,
        "gold_path": report.gold_path,
        "overall_pass": report.overall_pass,
        "grade": report.grade,
        "total_passed": report.total_passed,
        "total_criteria": report.total_criteria,
        "total_score": report.total_score,
        "max_score": report.max_score,
        "results": [
            {
                "id": r.id,
                "name": r.name,
                "weight": r.weight,
                "passed": r.passed,
                "score": r.score,
                "reasoning": r.reasoning,
                "quotes": r.quotes,
            }
            for r in report.results
        ],
    }
    path = output_dir / "results.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"JSON report: {path}")


def save_html(report: EvalReport, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for r in report.results:
        color = "#4eca8b" if r.passed else "#e85c5c"
        status = "PASS" if r.passed else "FAIL"
        weight = f' <span style="color:#e8a84c">*</span>' if r.weight == "critical" else ""
        quotes_html = ""
        if r.quotes:
            quotes_html = "<br>".join(f'<code>{html_mod.escape(q[:100])}</code>' for q in r.quotes[:3])
        rows.append(
            f'<tr><td>{html_mod.escape(r.name)}{weight}</td>'
            f'<td style="color:{color};font-weight:600">{status}</td>'
            f'<td>{r.score}/2</td>'
            f'<td>{html_mod.escape(r.reasoning)}</td>'
            f'<td>{quotes_html}</td></tr>'
        )

    overall_color = "#4eca8b" if report.overall_pass else "#e85c5c"
    overall_text = "PASS" if report.overall_pass else "FAIL"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Eval: {html_mod.escape(report.skill)}</title>
<style>
body {{ background:#0c0e12; color:#c8cdd8; font-family:'IBM Plex Mono',monospace; padding:2rem; }}
table {{ border-collapse:collapse; width:100%; margin:1rem 0; }}
th {{ text-align:left; padding:0.5rem; border-bottom:2px solid #3d4660; color:#6b7590; font-size:0.8rem; text-transform:uppercase; }}
td {{ padding:0.5rem; border-bottom:1px solid #2a3040; vertical-align:top; }}
code {{ background:rgba(91,141,239,0.08); color:#5b8def; padding:0.1em 0.3em; border-radius:3px; font-size:0.85em; }}
h1 {{ color:#e8ecf4; }}
.verdict {{ text-align:center; padding:1rem; margin:1rem 0; border:1px solid #2a3040; border-radius:6px; }}
.verdict span {{ font-size:1.5rem; font-weight:700; color:{overall_color}; }}
</style></head><body>
<h1>Evaluation: {html_mod.escape(report.skill)}</h1>
<p>Generated: <code>{html_mod.escape(report.generated_path)}</code><br>Gold: <code>{html_mod.escape(report.gold_path)}</code></p>
<div class="verdict"><span>{overall_text}</span> — Grade: {report.grade} | {report.total_passed}/{report.total_criteria} passed, score {report.total_score}/{report.max_score}</div>
<table>
<tr><th>Criterion</th><th>Result</th><th>Score</th><th>Reasoning</th><th>Evidence</th></tr>
{''.join(rows)}
</table>
</body></html>"""

    path = output_dir / "report.html"
    with open(path, "w") as f:
        f.write(html)
    print(f"HTML report: {path}")
