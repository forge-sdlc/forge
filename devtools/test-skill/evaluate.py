#!/usr/bin/env python3
"""
Forge skill output evaluator — judges generated artifacts against gold standards.

Usage:
    # Evaluate a single generated artifact against its gold standard
    python3 devtools/test-skill/evaluate.py \
        --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
        --generated output/enhancements/OSAC-1234/prd.md \
        --gold gold-prd.md \
        --output output/eval/

    # Evaluate after a runner batch (all cases in a dataset)
    python3 devtools/test-skill/evaluate.py \
        --criteria devtools/test-skill/evaluators/criteria/generate-prd.yaml \
        --dataset eval/dataset/cases/ \
        --results-dir output/ \
        --output output/eval/
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from evaluators.judge import evaluate, load_criteria
from evaluators.reports import print_terminal, save_json, save_html

try:
    import mlflow
    import mlflow.anthropic

    HAS_MLFLOW = True
except ImportError:
    HAS_MLFLOW = False


def find_generated_file(output_dir: Path, criteria_config: dict) -> Path | None:
    for pattern in criteria_config.get("generated_file_patterns", []):
        matches = list(output_dir.glob(pattern))
        if matches:
            return matches[0]
    for f in output_dir.rglob("prd.md"):
        return f
    return None


_mlflow_enabled = False


def run_single(
    criteria_path: Path,
    generated_path: Path,
    gold_path: Path,
    output_dir: Path,
):
    case_name = generated_path.parent.name or generated_path.stem

    print(f"Evaluating: {generated_path.name}")
    print(f"  Generated: {generated_path}")
    print(f"  Gold:      {gold_path}")

    if HAS_MLFLOW and _mlflow_enabled:
        with mlflow.start_run(run_name=f"eval — {case_name}"):
            report = evaluate(criteria_path, generated_path, gold_path)

            print_terminal(report)
            save_json(report, output_dir)
            save_html(report, output_dir)

            mlflow.set_tag("case", case_name)
            mlflow.set_tag("type", "evaluation")
            mlflow.set_tag("skill", report.skill)
            mlflow.set_tag("grade", report.grade)

            mlflow.log_metric("total_score", report.total_score)
            mlflow.log_metric("max_score", report.max_score)
            mlflow.log_metric("score_pct", round(report.total_score / report.max_score * 100, 1))
            mlflow.log_metric("criteria_passed", report.total_passed)
            mlflow.log_metric("criteria_total", report.total_criteria)
            mlflow.log_metric("critical_failures", len(report.critical_failures))

            for r in report.results:
                mlflow.log_metric(f"c_{r.id}", r.score)

            try:
                results_json = output_dir / "results.json"
                if results_json.exists():
                    mlflow.log_artifact(str(results_json), "eval")
            except Exception:
                pass

            print(f"  MLflow: logged eval for {case_name}")
    else:
        report = evaluate(criteria_path, generated_path, gold_path)
        print_terminal(report)
        save_json(report, output_dir)
        save_html(report, output_dir)

    return report


def run_batch(
    criteria_path: Path,
    dataset_dir: Path,
    results_dir: Path,
    output_dir: Path,
):
    config = load_criteria(criteria_path)
    reports = []

    for case_dir in sorted(dataset_dir.iterdir()):
        if not case_dir.is_dir():
            continue

        gold_file = case_dir / config.get("gold_standard_file", "gold-prd.md")
        if not gold_file.exists():
            print(f"Skipping {case_dir.name}: no gold standard")
            continue

        case_results = results_dir / case_dir.name
        if not case_results.exists():
            print(f"Skipping {case_dir.name}: no run results at {case_results}")
            continue

        generated = find_generated_file(case_results, config)
        if not generated:
            print(f"Skipping {case_dir.name}: no generated artifact found")
            continue

        case_output = output_dir / case_dir.name
        report = run_single(criteria_path, generated, gold_file, case_output)
        reports.append((case_dir.name, report))

    if reports:
        print(f"\n{'='*55}")
        print(f"Batch Summary: {len(reports)} cases evaluated")
        print(f"{'='*55}")
        for name, r in reports:
            status = "PASS" if r.overall_pass else "FAIL"
            print(f"  {name:<30} {r.grade}  {status}  {r.total_passed}/{r.total_criteria}  score {r.total_score}/{r.max_score}")


def main():
    global _mlflow_enabled

    parser = argparse.ArgumentParser(description="Forge skill output evaluator")
    parser.add_argument("--criteria", required=True, help="Path to criteria YAML file")
    parser.add_argument("--generated", help="Path to generated artifact")
    parser.add_argument("--gold", help="Path to gold standard artifact")
    parser.add_argument("--dataset", help="Path to dataset directory (batch mode)")
    parser.add_argument("--results-dir", help="Path to runner output directory (batch mode)")
    parser.add_argument("--output", required=True, help="Output directory for reports")
    parser.add_argument(
        "--mlflow",
        metavar="URI",
        help="MLflow tracking URI (e.g., http://host:5000). Logs eval scores as MLflow runs.",
    )
    parser.add_argument(
        "--mlflow-experiment",
        default="forge-skill-eval",
        help="MLflow experiment name (default: forge-skill-eval)",
    )
    args = parser.parse_args()

    if args.mlflow and HAS_MLFLOW:
        import logging
        logging.getLogger("mlflow.tracing.export").setLevel(logging.ERROR)
        mlflow.set_tracking_uri(args.mlflow)
        mlflow.set_experiment(args.mlflow_experiment)
        mlflow.anthropic.autolog()
        _mlflow_enabled = True
        print(f"MLflow: tracking to {args.mlflow}, experiment '{args.mlflow_experiment}'")

    criteria_path = Path(args.criteria)
    output_dir = Path(args.output)

    if args.generated and args.gold:
        run_single(criteria_path, Path(args.generated), Path(args.gold), output_dir)
    elif args.dataset and args.results_dir:
        run_batch(criteria_path, Path(args.dataset), Path(args.results_dir), output_dir)
    else:
        print("Error: provide either --generated + --gold, or --dataset + --results-dir")
        sys.exit(1)


if __name__ == "__main__":
    main()
