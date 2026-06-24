"""Task generation node for LangGraph workflow."""

import asyncio
import logging
import re
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient, MissingProjectConfig
from forge.models.workflow import ForgeLabel
from forge.prompts import load_prompt
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment

logger = logging.getLogger(__name__)


async def generate_tasks(state: WorkflowState) -> WorkflowState:
    """Generate implementation Tasks for each approved Epic.

    This node:
    1. Iterates through all Epics in epic_keys
    2. Generates Tasks for each Epic using Claude
    3. Creates Task tickets in Jira with repository labels
    4. Updates state with task tracking

    Args:
        state: Current workflow state with epic_keys.

    Returns:
        Updated state with task_keys and tasks_by_repo populated.
    """
    ticket_key = state["ticket_key"]
    epic_keys = state.get("epic_keys", [])

    if not epic_keys:
        logger.warning(f"No Epics found for task generation on {ticket_key}")
        return {
            **state,
            "last_error": "No Epics available for task generation",
            "current_node": "generate_tasks",
        }

    logger.info(f"Generating Tasks for {len(epic_keys)} Epics on {ticket_key}")

    settings = get_settings()
    jira = JiraClient()
    agent = ForgeAgent()

    await post_status_comment(
        jira,
        ticket_key,
        "⚙️ Forge is generating implementation tasks — this may take a few minutes.",
    )

    all_task_keys: list[str] = []
    tasks_by_repo: dict[str, list[str]] = {}
    # Track created tasks to provide context for subsequent epics (avoid duplication)
    created_tasks_context: list[dict[str, str]] = []
    # Pre-fetch all epic details so each epic can see its siblings' plans
    all_epics_details: list[dict[str, str]] = []
    jira_error = None

    spec_content = state.get("spec_content", "")

    try:
        # Get project key from parent Feature
        parent_issue = await jira.get_issue(ticket_key)
        project_key = parent_issue.project_key

        # Pre-fetch all epic details upfront for sibling context
        for ek in epic_keys:
            try:
                epic_issue = await jira.get_issue(ek)
                all_epics_details.append(
                    {
                        "epic_key": ek,
                        "epic_summary": epic_issue.summary,
                        "epic_plan": epic_issue.description or "",
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to pre-fetch Epic {ek}: {e}")
                all_epics_details.append({"epic_key": ek, "epic_summary": ek, "epic_plan": ""})

        for epic_key in epic_keys:
            logger.info(f"Generating Tasks for Epic {epic_key}")

            # Get Epic details from pre-fetched data
            epic_detail = next((e for e in all_epics_details if e["epic_key"] == epic_key), {})
            epic_plan = epic_detail.get("epic_plan", "")
            epic_summary = epic_detail.get("epic_summary", epic_key)

            if not epic_plan.strip():
                logger.warning(f"Epic {epic_key} has no implementation plan")
                continue

            # Get repo from Epic labels (set during decomposition)
            epic_labels = await jira.get_labels(epic_key)
            epic_repo = ""
            for label in epic_labels:
                if label.startswith("repo:"):
                    epic_repo = label[5:]
                    break

            # Build context
            context: dict[str, Any] = {
                "ticket_key": ticket_key,
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
                "epic_key": epic_key,
                "epic_summary": epic_summary,
                "feature_key": ticket_key,
                "project_key": project_key,
                "epic_repo": epic_repo,
                "feedback": state.get("feedback_comment", ""),
            }

            # Sibling epics = all epics except the current one
            sibling_epics = [e for e in all_epics_details if e["epic_key"] != epic_key]

            # Generate Tasks using Deep Agents - primary operation
            tasks_data = await _generate_tasks_for_epic(
                agent,
                epic_plan,
                epic_summary,
                context,
                spec_content=spec_content,
                sibling_epics=sibling_epics if sibling_epics else None,
                existing_tasks=created_tasks_context if created_tasks_context else None,
            )

            # Create Tasks in Jira - secondary operation
            for task in tasks_data:
                summary = task.get("summary", "Untitled Task")
                description = task.get("description", "")
                repo = task.get("repo", "")

                # Repo priority: task-level > epic-level > default config
                if not repo or repo == "unknown" or "/" not in repo:
                    repo = epic_repo  # Inherit from Epic

                if not repo or repo == "unknown" or "/" not in repo:
                    try:
                        repo = await jira.get_project_default_repo(project_key)
                    except MissingProjectConfig:
                        repo = (
                            settings.github_default_repo
                            if not settings.forge_require_project_config
                            else ""
                        )

                if not repo or "/" not in repo:
                    logger.warning(
                        f"Task '{summary}' has no valid repo. "
                        "Set repo labels on Feature/Epic or GITHUB_DEFAULT_REPO."
                    )
                    repo = "unknown"

                # Add labels: forge:managed for webhook routing, forge:parent for lookup, repo
                labels = [
                    ForgeLabel.FORGE_MANAGED.value,
                    f"forge:parent:{ticket_key}",  # Parent Feature key
                ]
                if repo and repo != "unknown":
                    labels.append(f"repo:{repo}")

                try:
                    task_key = await jira.create_task(
                        project_key=project_key,
                        summary=summary,
                        description=description,
                        parent_key=epic_key,
                        labels=labels,
                    )

                    all_task_keys.append(task_key)

                    # Track by repository
                    if repo not in tasks_by_repo:
                        tasks_by_repo[repo] = []
                    tasks_by_repo[repo].append(task_key)

                    # Track for context in subsequent epic task generation
                    created_tasks_context.append(
                        {
                            "epic_key": epic_key,
                            "epic_summary": epic_summary,
                            "task_key": task_key,
                            "summary": summary,
                        }
                    )

                    logger.info(f"Created Task {task_key}: {summary} (repo: {repo})")
                except Exception as e:
                    # Log but continue creating remaining Tasks
                    jira_error = str(e)
                    logger.warning(f"Failed to create Task '{summary}' for {ticket_key}: {e}")

        logger.info(
            f"Created {len(all_task_keys)} Tasks for {ticket_key}, awaiting implementation approval"
        )

        # If we created some Tasks, advance even with partial failures
        if all_task_keys:
            # Only set workflow label after confirming tasks were created
            try:
                await jira.set_workflow_label(ticket_key, ForgeLabel.TASK_PENDING)
            except Exception as e:
                jira_error = str(e)
                logger.warning(f"Failed to set workflow label for {ticket_key}: {e}")
            return update_state_timestamp(
                {
                    **state,
                    "task_keys": all_task_keys,
                    "tasks_by_repo": tasks_by_repo,
                    "feedback_comment": None,
                    "revision_requested": False,
                    "current_task_key": None,
                    "current_epic_key": None,
                    "current_node": "task_approval_gate",
                    "last_error": f"Partial Jira failure: {jira_error}" if jira_error else None,
                }
            )
        else:
            # No Tasks created at all - this is a failure
            return {
                **state,
                "last_error": jira_error or "Failed to create any Tasks in Jira",
                "current_node": "generate_tasks",
                "retry_count": state.get("retry_count", 0) + 1,
            }

    except Exception as e:
        logger.error(f"Task generation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "generate_tasks")
        # Save any Tasks we managed to create
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "generate_tasks",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if all_task_keys:
            result_state["task_keys"] = all_task_keys
            result_state["tasks_by_repo"] = tasks_by_repo
        return result_state
    finally:
        await jira.close()


async def _generate_tasks_for_epic(
    agent: ForgeAgent,
    epic_plan: str,
    epic_summary: str,
    context: dict[str, Any],
    spec_content: str = "",
    sibling_epics: list[dict[str, str]] | None = None,
    existing_tasks: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Generate Tasks for a single Epic.

    Args:
        agent: Deep Agent client.
        epic_plan: Epic implementation plan.
        epic_summary: Epic title/summary.
        context: Additional context.
        spec_content: Full approved specification for the feature.
        sibling_epics: Other epics in this feature (summary + plan) for context.
        existing_tasks: Tasks already created for sibling epics (to avoid duplication).

    Returns:
        List of Task dicts with summary, description, repo.
    """
    existing_tasks_section = _format_existing_tasks(existing_tasks)
    sibling_epics_section = _format_sibling_epics(sibling_epics)

    prompt = load_prompt(
        "generate-tasks",
        epic_summary=epic_summary,
        epic_plan=epic_plan,
        spec_content=spec_content[:4000] if spec_content else "Not available.",
        sibling_epics_section=sibling_epics_section,
        existing_tasks_section=existing_tasks_section,
    )

    feedback = context.get("feedback", "") if context else ""
    if feedback:
        prompt += (
            f"\n\n## Revision Feedback\n\n"
            f"The user has requested the following changes to the tasks for this Epic:\n{feedback}\n\n"
            f"Please incorporate this feedback when creating the tasks."
        )

    result = await agent.run_task(
        task="generate-tasks",
        prompt=prompt,
        context=context,
    )

    return _parse_tasks_response(result)


def _format_sibling_epics(sibling_epics: list[dict[str, str]] | None) -> str:
    """Format sibling epics for prompt context.

    Args:
        sibling_epics: List of dicts with epic_key, epic_summary, epic_plan.

    Returns:
        Formatted string, or empty string if no siblings.
    """
    if not sibling_epics:
        return "None — this is the only Epic in the feature."

    lines = []
    for epic in sibling_epics:
        key = epic.get("epic_key", "")
        summary = epic.get("epic_summary", "")
        plan = epic.get("epic_plan", "")
        lines.append(f"### {key}: {summary}")
        if plan:
            # Include up to 500 chars of each sibling plan to keep prompt size reasonable
            lines.append(plan[:500] + ("..." if len(plan) > 500 else ""))
        lines.append("")

    return "\n".join(lines)


def _format_existing_tasks(existing_tasks: list[dict[str, str]] | None) -> str:
    """Format existing tasks from sibling epics for prompt context.

    Args:
        existing_tasks: List of task dicts with epic_key, task_key, summary.

    Returns:
        Formatted string for prompt, or empty string if no existing tasks.
    """
    if not existing_tasks:
        return ""

    # Group tasks by epic
    tasks_by_epic: dict[str, list[dict[str, str]]] = {}
    for task in existing_tasks:
        epic_key = task.get("epic_key", "Unknown")
        if epic_key not in tasks_by_epic:
            tasks_by_epic[epic_key] = []
        tasks_by_epic[epic_key].append(task)

    lines = ["## Existing Tasks from Other Epics", ""]
    for epic_key, tasks in tasks_by_epic.items():
        epic_summary = tasks[0].get("epic_summary", "")
        lines.append(f"{epic_key} ({epic_summary}):")
        for task in tasks:
            lines.append(f"- {task.get('task_key', '???')}: {task.get('summary', 'Untitled')}")
        lines.append("")

    return "\n".join(lines)


def _parse_tasks_response(response: str) -> list[dict[str, str]]:
    """Parse Task generation response into structured data.

    Args:
        response: Raw response from Claude.

    Returns:
        List of Task dicts.
    """
    tasks = []
    current_task: dict[str, str] = {}
    current_section = None
    section_lines: list[str] = []

    for line in response.split("\n"):
        stripped = line.strip()

        if stripped.startswith("---"):
            # Save previous task if exists
            if current_task.get("summary"):
                if current_section == "description":
                    current_task["description"] = "\n".join(section_lines).strip()
                elif current_section == "acceptance_criteria":
                    # Append acceptance criteria to description
                    criteria = "\n".join(section_lines).strip()
                    current_task["description"] = (
                        current_task.get("description", "")
                        + "\n\nAcceptance Criteria:\n"
                        + criteria
                    ).strip()
                tasks.append(current_task)
                current_task = {}
                section_lines = []
            continue

        if stripped.startswith("TASK:"):
            current_task["summary"] = stripped[5:].strip()
            current_section = "summary"
        elif stripped.startswith("REPO:"):
            repo = stripped[5:].strip().lower()
            # Clean up repo name
            repo = re.sub(r"[^a-z0-9\-_]", "", repo)
            current_task["repo"] = repo if repo else "unknown"
        elif stripped.startswith("DESCRIPTION:"):
            current_section = "description"
            section_lines = []
        elif stripped.startswith("ACCEPTANCE_CRITERIA:"):
            # Save description first
            if current_section == "description":
                current_task["description"] = "\n".join(section_lines).strip()
            current_section = "acceptance_criteria"
            section_lines = []
        elif current_section in ("description", "acceptance_criteria"):
            section_lines.append(line)

    # Don't forget the last task
    if current_task.get("summary"):
        if current_section == "description":
            current_task["description"] = "\n".join(section_lines).strip()
        elif current_section == "acceptance_criteria":
            criteria = "\n".join(section_lines).strip()
            current_task["description"] = (
                current_task.get("description", "") + "\n\nAcceptance Criteria:\n" + criteria
            ).strip()
        tasks.append(current_task)

    return tasks


def extract_repo_from_labels(labels: list[str]) -> str:
    """Extract repository name from Jira labels.

    Args:
        labels: List of Jira labels.

    Returns:
        Repository name or "unknown".
    """
    for label in labels:
        if label.startswith("repo:"):
            return label[5:]
    return "unknown"


async def regenerate_all_tasks(state: WorkflowState) -> WorkflowState:
    """Delete all Tasks and regenerate from Epics with feedback.

    This handles Feature-level rejection where the entire Task
    breakdown needs to be revised.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new task_keys.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    existing_tasks = state.get("task_keys", [])

    logger.info(f"Regenerating all Tasks for {ticket_key} with feedback")

    jira = JiraClient()

    try:
        # Archive existing Tasks (unlink from parent, mark as archived)
        for task_key in existing_tasks:
            try:
                await jira.archive_issue(task_key, archive_subtasks=False)
                logger.info(f"Archived Task {task_key}")
            except Exception as e:
                logger.warning(f"Failed to archive Task {task_key}: {e}")

        # Clear task_keys and set feedback for regeneration
        updated_state = {
            **state,
            "task_keys": [],
            "tasks_by_repo": {},
            "feedback_comment": feedback,
        }

        # Re-run task generation (which will incorporate feedback in context)
        return await generate_tasks(updated_state)

    except Exception as e:
        logger.error(f"Task regeneration failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_all_tasks")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_all_tasks",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


async def regenerate_epic_tasks(state: WorkflowState) -> WorkflowState:
    """Archive and regenerate tasks for a single Epic with feedback.

    Handles Epic-level rejection at task_approval_gate: only the tasks
    belonging to current_epic_key are replaced. Tasks from other Epics
    are preserved in state.

    Args:
        state: Workflow state with current_epic_key and feedback_comment set.

    Returns:
        Updated state with new tasks merged in for the target Epic.
    """
    ticket_key = state["ticket_key"]
    epic_key = state.get("current_epic_key")
    feedback = state.get("feedback_comment", "")
    existing_task_keys = state.get("task_keys", [])
    existing_tasks_by_repo = dict(state.get("tasks_by_repo", {}))

    if not epic_key:
        logger.warning(f"No current_epic_key for epic task regeneration on {ticket_key}")
        return {
            **state,
            "feedback_comment": None,
            "revision_requested": False,
            "current_node": "task_approval_gate",
        }

    logger.info(f"Regenerating tasks for Epic {epic_key} on {ticket_key} with feedback")

    settings = get_settings()
    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Identify which tasks belong to this epic (fetched concurrently)
        epic_task_keys: list[str] = []
        parent_issue = await jira.get_issue(ticket_key)
        project_key = parent_issue.project_key

        async def _check_task_parent(task_key: str) -> str | None:
            try:
                task_issue = await jira.get_issue(task_key)
                if task_issue.parent_key is None:
                    logger.warning(
                        f"Task {task_key} has no parent in Jira; skipping archive "
                        f"(cannot confirm it belongs to {epic_key})"
                    )
                    return None
                return task_key if task_issue.parent_key == epic_key else None
            except Exception as e:
                logger.warning(f"Could not check parent of {task_key}: {e}")
                return None

        parent_results = await asyncio.gather(*(_check_task_parent(k) for k in existing_task_keys))
        epic_task_keys = [k for k in parent_results if k is not None]

        # Compute remaining tasks from other epics
        remaining_task_keys = [k for k in existing_task_keys if k not in epic_task_keys]
        remaining_tasks_by_repo: dict[str, list[str]] = {}
        for repo, keys in existing_tasks_by_repo.items():
            kept = [k for k in keys if k not in epic_task_keys]
            if kept:
                remaining_tasks_by_repo[repo] = kept

        # Fetch epic details
        epic_issue = await jira.get_issue(epic_key)
        epic_plan = epic_issue.description or ""
        epic_summary = epic_issue.summary
        epic_labels = await jira.get_labels(epic_key)
        epic_repo = extract_repo_from_labels(epic_labels)

        # Fetch sibling epics for context (concurrently)
        all_epic_keys = state.get("epic_keys", [])
        sibling_keys = [ek for ek in all_epic_keys if ek != epic_key]

        async def _fetch_sibling(ek: str) -> dict[str, str] | None:
            try:
                sib = await jira.get_issue(ek)
                return {
                    "epic_key": ek,
                    "epic_summary": sib.summary,
                    "epic_plan": sib.description or "",
                }
            except Exception as e:
                logger.warning(f"Failed to fetch sibling epic {ek}: {e}")
                return None

        sibling_results = await asyncio.gather(*(_fetch_sibling(ek) for ek in sibling_keys))
        sibling_epics: list[dict[str, str]] = [s for s in sibling_results if s is not None]

        # Fetch remaining tasks for existing-tasks context (dedup)
        existing_tasks_ctx: list[dict[str, str]] = []
        for task_key in remaining_task_keys:
            try:
                task_issue = await jira.get_issue(task_key)
                existing_tasks_ctx.append(
                    {
                        "epic_key": task_issue.parent_key or "",
                        "epic_summary": "",
                        "task_key": task_key,
                        "summary": task_issue.summary,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to fetch remaining task {task_key}: {e}")

        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "ticket_type": state.get("ticket_type", ""),
            "current_node": state.get("current_node", ""),
            "event_type": state.get("event_type", ""),
            "event_source": state.get("context", {}).get("source", ""),
            "retry_count": state.get("retry_count", 0),
            "epic_key": epic_key,
            "epic_summary": epic_summary,
            "feature_key": ticket_key,
            "project_key": project_key,
            "epic_repo": epic_repo,
            "feedback": feedback,
        }

        spec_content = state.get("spec_content", "")

        tasks_data = await _generate_tasks_for_epic(
            agent,
            epic_plan,
            epic_summary,
            context,
            spec_content=spec_content,
            sibling_epics=sibling_epics if sibling_epics else None,
            existing_tasks=existing_tasks_ctx if existing_tasks_ctx else None,
        )

        if not tasks_data:
            return {
                **state,
                "last_error": f"No replacement Tasks generated for Epic {epic_key}",
                "current_node": "regenerate_epic_tasks",
                "retry_count": state.get("retry_count", 0) + 1,
                "revision_requested": False,
                "feedback_comment": None,
                "current_epic_key": None,
            }

        # Create new tasks in Jira under this epic
        new_task_keys: list[str] = []
        jira_error = None

        for task in tasks_data:
            summary = task.get("summary", "Untitled Task")
            description = task.get("description", "")
            repo = task.get("repo", "")

            if not repo or repo == "unknown" or "/" not in repo:
                repo = epic_repo
            if not repo or repo == "unknown" or "/" not in repo:
                try:
                    repo = await jira.get_project_default_repo(project_key)
                except MissingProjectConfig:
                    repo = (
                        settings.github_default_repo
                        if not settings.forge_require_project_config
                        else ""
                    )
            if not repo or "/" not in repo:
                repo = "unknown"

            labels = [
                ForgeLabel.FORGE_MANAGED.value,
                f"forge:parent:{ticket_key}",
            ]
            if repo and repo != "unknown":
                labels.append(f"repo:{repo}")

            try:
                task_key = await jira.create_task(
                    project_key=project_key,
                    summary=summary,
                    description=description,
                    parent_key=epic_key,
                    labels=labels,
                )
                new_task_keys.append(task_key)
                remaining_tasks_by_repo.setdefault(repo, []).append(task_key)
                logger.info(f"Created Task {task_key}: {summary} (repo: {repo})")
            except Exception as e:
                jira_error = str(e)
                logger.warning(f"Failed to create Task '{summary}' for {epic_key}: {e}")

        if not new_task_keys:
            return {
                **state,
                "last_error": jira_error
                or f"Failed to create replacement Tasks for Epic {epic_key}",
                "current_node": "regenerate_epic_tasks",
                "retry_count": state.get("retry_count", 0) + 1,
                "revision_requested": False,
                "feedback_comment": None,
                "current_epic_key": None,
            }

        if jira_error:
            cleanup_errors: list[str] = []
            for task_key in new_task_keys:
                try:
                    await jira.archive_issue(task_key, archive_subtasks=False)
                    logger.info(f"Archived partially created replacement Task {task_key}")
                except Exception as e:
                    cleanup_errors.append(f"{task_key}: {e}")
                    logger.warning(
                        f"Failed to archive partially created replacement Task {task_key}: {e}"
                    )

            cleanup_suffix = (
                f"; cleanup failures: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
            )
            return {
                **state,
                "last_error": (
                    f"Partial replacement Task creation failed for Epic {epic_key}: "
                    f"{jira_error}{cleanup_suffix}"
                ),
                "current_node": "regenerate_epic_tasks",
                "retry_count": state.get("retry_count", 0) + 1,
                "revision_requested": False,
                "feedback_comment": None,
                "current_epic_key": None,
            }

        # Archive only after replacement tasks exist, so a bad generation cannot
        # leave the target Epic with no implementation tasks.
        for task_key in epic_task_keys:
            try:
                await jira.archive_issue(task_key, archive_subtasks=False)
                logger.info(f"Archived Task {task_key}")
            except Exception as e:
                logger.warning(f"Failed to archive Task {task_key}: {e}")

        all_task_keys = remaining_task_keys + new_task_keys
        logger.info(f"Regenerated {len(new_task_keys)} tasks for Epic {epic_key} on {ticket_key}")

        return update_state_timestamp(
            {
                **state,
                "task_keys": all_task_keys,
                "tasks_by_repo": remaining_tasks_by_repo,
                "feedback_comment": None,
                "revision_requested": False,
                "current_epic_key": None,
                "current_node": "task_approval_gate",
                "last_error": f"Partial Jira failure: {jira_error}" if jira_error else None,
            }
        )

    except Exception as e:
        logger.error(f"Epic task regeneration failed for {epic_key} on {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_epic_tasks")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_epic_tasks",
            "retry_count": state.get("retry_count", 0) + 1,
            # Clear revision flags so task_approval_gate returns END instead of looping
            "revision_requested": False,
            "feedback_comment": None,
            "current_epic_key": None,
        }
    finally:
        await jira.close()
        await agent.close()


async def update_single_task(state: WorkflowState) -> WorkflowState:
    """Update a single Task's description based on feedback.

    This handles Task-level feedback where only one Task needs revision.

    Args:
        state: Workflow state with current_task_key and feedback_comment.

    Returns:
        Updated state.
    """
    ticket_key = state["ticket_key"]
    task_key = state.get("current_task_key")
    feedback = state.get("feedback_comment", "")

    if not task_key:
        logger.warning(f"No current_task_key for single Task update on {ticket_key}")
        return state

    logger.info(f"Updating Task {task_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Get current Task description
        task_issue = await jira.get_issue(task_key)
        original_description = task_issue.description or ""

        # Regenerate description with feedback
        new_description = await agent.regenerate_with_feedback(
            original_content=original_description,
            feedback=feedback,
            content_type="task",
            ticket_key=ticket_key,
            context={
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
        )

        # Update Task in Jira
        await jira.update_description(task_key, new_description)

        # Add comment acknowledging revision
        await jira.add_comment(
            task_key,
            "Task has been revised based on feedback. Please review.",
        )

        logger.info(f"Task {task_key} updated with feedback")

        return update_state_timestamp(
            {
                **state,
                "current_task_key": None,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "task_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Task update failed for {task_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "update_single_task")
        return {
            **state,
            "last_error": str(e),
            "current_node": "update_single_task",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()
