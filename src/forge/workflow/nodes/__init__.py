"""LangGraph node implementations for workflow phases."""

from forge.workflow.nodes.ci_evaluator import (
    attempt_ci_fix,
    escalate_to_blocked,
    evaluate_ci_status,
    wait_for_ci_gate,
)
from forge.workflow.nodes.docs_updater import update_documentation
from forge.workflow.nodes.epic_decomposition import (
    check_all_epics_approved,
    decompose_epics,
    regenerate_all_epics,
    update_single_epic,
)
from forge.workflow.nodes.error_handler import notify_error
from forge.workflow.nodes.human_review import (
    aggregate_epic_status,
    aggregate_feature_status,
    complete_tasks,
    human_review_gate,
    route_human_review,
)
from forge.workflow.nodes.implementation import implement_task
from forge.workflow.nodes.local_reviewer import local_review_changes
from forge.workflow.nodes.plan_bug_fix import (
    decompose_plan,
    plan_approval_gate,
    plan_bug_fix,
    regenerate_plan,
    route_plan_approval,
)
from forge.workflow.nodes.post_merge_summary import post_merge_summary
from forge.workflow.nodes.pr_creation import (
    create_pull_request,
    teardown_and_route,
)
from forge.workflow.nodes.prd_generation import (
    generate_prd,
    regenerate_prd_with_feedback,
)
from forge.workflow.nodes.qa_handler import answer_question, extract_question_text
from forge.workflow.nodes.rca_analysis import analyze_bug, reflect_rca
from forge.workflow.nodes.rca_option_gate import (
    rca_option_gate,
    regenerate_rca,
    route_rca_option,
)
from forge.workflow.nodes.rebase import rebase_pr
from forge.workflow.nodes.spec_generation import (
    generate_spec,
    regenerate_spec_with_feedback,
)
from forge.workflow.nodes.task_generation import (
    extract_repo_from_labels,
    generate_tasks,
)
from forge.workflow.nodes.task_router import (
    ParallelExecutionTracker,
    aggregate_parallel_results,
    get_repo_execution_plan,
    route_tasks_by_repo,
    route_tasks_parallel,
    should_use_parallel_execution,
)
from forge.workflow.nodes.triage import route_triage_gate, triage_check, triage_gate
from forge.workflow.nodes.workspace_setup import (
    get_workspace_manager,
    setup_workspace,
    teardown_workspace,
)

__all__ = [
    # PRD generation
    "generate_prd",
    "regenerate_prd_with_feedback",
    # Spec generation
    "generate_spec",
    "regenerate_spec_with_feedback",
    # Epic decomposition
    "check_all_epics_approved",
    "decompose_epics",
    "regenerate_all_epics",
    "update_single_epic",
    # Task generation
    "extract_repo_from_labels",
    "generate_tasks",
    # Task routing and parallel execution
    "ParallelExecutionTracker",
    "aggregate_parallel_results",
    "get_repo_execution_plan",
    "route_tasks_by_repo",
    "route_tasks_parallel",
    "should_use_parallel_execution",
    # Workspace management
    "get_workspace_manager",
    "setup_workspace",
    "teardown_workspace",
    # Implementation
    "implement_task",
    # PR creation
    "create_pull_request",
    "teardown_and_route",
    # CI/CD evaluation
    "attempt_ci_fix",
    "escalate_to_blocked",
    "evaluate_ci_status",
    "wait_for_ci_gate",
    # Documentation update
    "update_documentation",
    # Local code review
    "local_review_changes",
    # Human review
    "aggregate_epic_status",
    "aggregate_feature_status",
    "complete_tasks",
    "human_review_gate",
    "route_human_review",
    # Bug workflow — triage
    "triage_check",
    "triage_gate",
    "route_triage_gate",
    # Bug workflow — RCA analysis
    "analyze_bug",
    "reflect_rca",
    # Bug workflow — RCA option gate
    "rca_option_gate",
    "regenerate_rca",
    "route_rca_option",
    # Bug workflow — planning
    "decompose_plan",
    "plan_approval_gate",
    "plan_bug_fix",
    "regenerate_plan",
    "route_plan_approval",
    # Bug workflow — implementation
    # Bug workflow — post-merge
    "post_merge_summary",
    # Error handling
    "notify_error",
    # Q&A handling
    "answer_question",
    "extract_question_text",
    # Rebase (merge conflict resolution)
    "rebase_pr",
]
