"""Documentation update node — finds and fixes stale docs before PR creation."""

import logging
from pathlib import Path

from forge.config import get_settings
from forge.prompts import load_prompt
from forge.sandbox import ContainerRunner
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workspace.git_ops import GitOperations
from forge.workspace.manager import Workspace

logger = logging.getLogger(__name__)


async def update_documentation(state: WorkflowState) -> WorkflowState:
    """Find and update documentation files that became stale due to code changes.

    Runs after local code review but before PR creation. Spawns a container
    that diffs the branch against main, discovers doc files, greps for
    changed identifiers, and applies minimal targeted updates to stale docs.

    Non-blocking: failures log a warning and proceed to PR creation.
    Documentation update issues should not block code delivery.

    Args:
        state: Current workflow state.

    Returns:
        Updated state routing to create_pr.
    """
    ticket_key = state["ticket_key"]
    workspace_path = state.get("workspace_path")

    if not workspace_path:
        logger.info(f"No workspace for doc update on {ticket_key}, skipping")
        return update_state_timestamp({**state, "current_node": "create_pr"})

    logger.info(f"Running documentation update for {ticket_key}")

    settings = get_settings()
    guardrails = state.get("context", {}).get("guardrails", "")
    current_repo = state.get("current_repo", "")
    branch_name = state.get("context", {}).get("branch_name", "")

    task_description = load_prompt(
        "update-docs",
        workspace_path=workspace_path,
        guardrails=guardrails[:2000] if guardrails else "",
    )

    try:
        runner = ContainerRunner(settings)
        result = await runner.run(
            workspace_path=Path(workspace_path),
            task_summary="Update stale documentation",
            task_description=task_description,
            ticket_key=ticket_key,
            task_key=f"{ticket_key}-docs",
            repo_name=current_repo,
            step_name="update_docs",
        )

        git = GitOperations(
            Workspace(
                path=Path(workspace_path),
                repo_name=current_repo,
                branch_name=branch_name,
                ticket_key=ticket_key,
            )
        )

        if git.has_uncommitted_changes():
            git.stage_all()
            git.commit(f"[{ticket_key}] docs: update documentation for code changes")
            logger.info(f"Committed doc updates for {ticket_key}")

        if result.success:
            logger.info(f"Documentation update completed for {ticket_key}")
        else:
            logger.warning(
                f"Documentation update container exited with errors for {ticket_key}, "
                f"proceeding to PR creation"
            )

        return update_state_timestamp(
            {
                **state,
                "current_node": "create_pr",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.warning(f"Documentation update failed for {ticket_key}: {e}")
        return update_state_timestamp(
            {
                **state,
                "current_node": "create_pr",
                "last_error": None,
            }
        )
