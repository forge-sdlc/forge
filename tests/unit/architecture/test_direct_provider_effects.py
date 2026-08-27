"""Keep the legacy workflow mutation inventory monotonically decreasing."""

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / "src" / "forge" / "workflow"
MUTATION_PREFIXES = (
    "create_",
    "update_",
    "delete_",
    "add_",
    "remove_",
    "set_",
    "transition_",
    "archive_",
    "reply_",
    "put_",
)
BASELINE = {
    "nodes/ci_evaluator.py": 1,
    "nodes/code_review.py": 1,
    "nodes/epic_decomposition.py": 7,
    "nodes/error_handler.py": 3,
    "nodes/human_review.py": 5,
    "nodes/implement_review.py": 2,
    "nodes/plan_bug_fix.py": 4,
    "nodes/post_merge_summary.py": 1,
    "nodes/pr_creation.py": 6,
    "nodes/prd_generation.py": 7,
    "nodes/proposal_pr.py": 7,
    "nodes/qa_handler.py": 2,
    "nodes/rca_analysis.py": 1,
    "nodes/rca_option_gate.py": 2,
    "nodes/rebase.py": 2,
    "nodes/spec_generation.py": 11,
    "nodes/task_generation.py": 8,
    "nodes/task_takeover_planning.py": 4,
    "nodes/task_takeover_triage.py": 1,
    "nodes/triage.py": 1,
    "task_takeover/graph.py": 2,
    "utils/jira_status.py": 6,
    "utils/qa_summary.py": 1,
    "utils/repo_resolution.py": 1,
    "utils/review_decisions.py": 1,
}


def test_direct_workflow_provider_mutations_only_decline() -> None:
    observed: Counter[str] = Counter()
    for path in WORKFLOW.rglob("*.py"):
        relative = path.relative_to(WORKFLOW).as_posix()
        if relative == "declarative/cli.py":
            continue  # Operator configuration is not workflow execution.
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            call = node.value if isinstance(node, ast.Await) else None
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr.startswith(MUTATION_PREFIXES)
            ):
                observed[relative] += 1

    unexpected = {
        path: count
        for path, count in observed.items()
        if path not in BASELINE or count > BASELINE[path]
    }
    assert unexpected == {}, (
        "Direct workflow provider mutations must be replaced by durable effects; "
        f"the baseline may never grow: {unexpected}"
    )
