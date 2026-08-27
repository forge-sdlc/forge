"""Generic task-first implementation node with planning-artifact fallback."""

import logging
from typing import Any

from forge.config import get_settings
from forge.integrations.jira.client import JiraClient
from forge.sandbox.runner import ContainerRunner
from forge.workflow.implementation_input import (
    NoPendingImplementationWork,
    resolve_implementation_input,
)
from forge.workflow.nodes.execution_engine import (
    ExecutionArtifact,
    ExecutionPersistenceError,
    ExecutionRequest,
    build_execution_prompt,
    run_and_persist_execution,
)
from forge.workflow.nodes.git_persistence import (
    PushPersistenceError,
    build_persistence_error_state,
    push_to_fork_with_retry,
)
from forge.workflow.nodes.workspace_setup import prepare_workspace
from forge.workflow.planning_state import repository_compatibility_update
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.references import fetch_and_inject_references

logger = logging.getLogger(__name__)


async def implement_work(state: dict[str, Any]) -> dict[str, Any]:
    """Implement the most specific repository-scoped work that is available."""
    ticket_key = state["ticket_key"]
    state = {**state, **repository_compatibility_update(state)}
    current_repo = state.get("current_repository") or ""
    node_name = "implement_work"
    jira = JiraClient(get_settings())
    container_started = False

    try:
        workspace_path, git = prepare_workspace(state)
        state = {**state, "workspace_path": workspace_path}

        if state.get("implementation_push_pending"):
            try:
                await push_to_fork_with_retry(git)
            except PushPersistenceError as exc:
                return update_state_timestamp(
                    build_persistence_error_state(state, exc, retry_node=node_name)
                )
            pending_id = state.get("implementation_push_pending_task")
            completed_units = list(state.get("work_units") or [])
            pending_kind = None
            for unit in completed_units:
                if unit.get("id") == pending_id:
                    unit["status"] = "completed"
                    pending_kind = unit.get("kind")
            implemented = list(state.get("implemented_tasks") or [])
            if pending_kind == "task" and pending_id and pending_id not in implemented:
                implemented.append(pending_id)
            return update_state_timestamp(
                {
                    **state,
                    "work_units": completed_units,
                    "implemented_tasks": implemented,
                    "current_task_key": None,
                    "current_work_unit_id": None,
                    "current_node": node_name,
                    "implementation_push_pending": False,
                    "implementation_push_pending_task": None,
                    "persistence_retry_count": 0,
                    "last_error": None,
                }
            )

        try:
            resolved = await resolve_implementation_input(state, jira)
        except NoPendingImplementationWork:
            return update_state_timestamp(
                {
                    **state,
                    "current_node": "local_review",
                    "current_work_unit_id": None,
                    "local_review_pass_number": 1,
                    "last_error": None,
                }
            )

        work_unit = resolved.work_unit
        work_id = work_unit["id"]
        primary_id = work_unit["source_artifact_ids"][0]
        supporting = tuple(
            ExecutionArtifact(
                title=str(artifact.get("kind", "artifact")).replace("_", " ").title(),
                content=str(artifact.get("content", "")),
            )
            for artifact in resolved.context_artifacts
            if artifact.get("id") != primary_id and artifact.get("content")
        )
        source_kind = str(work_unit.get("kind", "artifact"))
        summary = resolved.summary or f"Implement {source_kind} work for {ticket_key}"
        request = ExecutionRequest(
            ticket_key=ticket_key,
            work_id=work_id,
            repository=current_repo,
            workspace_path=workspace_path,
            summary=summary,
            description=resolved.instructions,
            description_title=f"Selected {source_kind.replace('_', ' ').title()}",
            node_name=node_name,
            step_name=node_name,
            policy_key="implement_task",
            commit_message=f"[{ticket_key}] implement {source_kind} work for {current_repo}",
            artifacts=supporting,
            critical_instructions=(
                "Read and understand the existing codebase before changing it.",
                "Implement only the selected work belonging to the current repository.",
                "Add or update tests for the behavior you change.",
                "Run the relevant build and test commands before finishing.",
            ),
        )
        prompt = await fetch_and_inject_references(
            state,
            jira,
            build_execution_prompt(request),
        )
        await post_status_comment(
            jira,
            ticket_key,
            f"🔨 Forge is implementing `{work_id}` in `{current_repo}` using "
            f"{source_kind.replace('_', ' ')} context.",
        )

        container_started = True
        try:
            execution_state = await run_and_persist_execution(
                {**state, **resolved.state_update(state)},
                request,
                runner=ContainerRunner(get_settings()),
                git=git,
                prompt=prompt,
            )
        except ExecutionPersistenceError as exc:
            container_started = False
            return update_state_timestamp(
                build_persistence_error_state(
                    {
                        **exc.state,
                        "implementation_push_pending": True,
                        "implementation_push_pending_task": work_id,
                    },
                    exc.cause,
                    retry_node=node_name,
                )
            )
        container_started = False

        if execution_state.get("last_error"):
            return update_state_timestamp(execution_state)

        completed_units = list(execution_state.get("work_units") or [])
        for unit in completed_units:
            if unit.get("id") == work_id:
                unit["status"] = "completed"
        implemented = list(execution_state.get("implemented_tasks") or [])
        if source_kind == "task" and work_id not in implemented:
            implemented.append(work_id)
        return update_state_timestamp(
            {
                **execution_state,
                "work_units": completed_units,
                "implemented_tasks": implemented,
                "current_task_key": None,
                "current_work_unit_id": None,
                "current_node": node_name,
                "last_error": None,
                "retry_count": 0,
            }
        )
    except Exception as exc:
        logger.error("Generic implementation failed for %s: %s", ticket_key, exc)
        return update_state_timestamp(
            {
                **state,
                "last_error": str(exc),
                "current_node": node_name,
                "retry_count": state.get("retry_count", 0) + 1,
            }
        )
    finally:
        if container_started:
            logger.warning("Implementation container did not complete cleanly for %s", ticket_key)
        await jira.close()
