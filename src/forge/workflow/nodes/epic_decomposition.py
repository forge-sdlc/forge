"""Epic decomposition node for LangGraph workflow."""

import logging
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient, MissingProjectConfig
from forge.models.workflow import ForgeLabel
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

logger = logging.getLogger(__name__)


def _missing_repo_config_comment(project_key: str) -> str:
    return (
        f"⚠️ Forge configuration required for project {project_key}\n\n"
        "This ticket cannot be processed because no repository configuration "
        "has been set for this Jira project.\n\n"
        "To fix this, a Jira project admin must set the following project property:\n\n"
        "  Key:   forge.repos\n"
        '  Value: ["owner/repo-name", "owner/other-repo"]\n\n'
        "Optionally, also set:\n\n"
        "  Key:   forge.default_repo\n"
        '  Value: "owner/repo-name"\n\n'
        "Once set, add the label `forge:retry` to this ticket to resume."
    )


async def decompose_epics(state: WorkflowState) -> WorkflowState:
    """Decompose specification into logical Epics with implementation plans.

    This node:
    1. Reads the approved specification from state
    2. Generates 2-5 cohesive Epics using Claude
    3. Creates Epic tickets in Jira linked to parent Feature
    4. Transitions Feature to "Pending Plan Approval"

    Args:
        state: Current workflow state with spec_content.

    Returns:
        Updated state with epic_keys populated.
    """
    ticket_key = state["ticket_key"]
    spec_content = state.get("spec_content", "")

    logger.info(f"Decomposing spec into Epics for {ticket_key}")

    # Post Q&A summary for spec if any
    qa_history = state.get("qa_history", [])
    if qa_history:
        await post_qa_summary_if_needed(ticket_key, qa_history, "spec")

    jira = JiraClient()
    agent = ForgeAgent()
    epic_keys: list[str] = []
    jira_error = None

    try:
        await post_status_comment(
            jira,
            ticket_key,
            "🗺️ Forge is decomposing your spec into an implementation plan — this may take a few minutes.",
        )

        # If spec not in state, this is an error
        if not spec_content.strip():
            logger.error(f"No spec content found for {ticket_key}")
            return {
                **state,
                "last_error": "No spec content available for Epic decomposition",
                "current_node": "decompose_epics",
            }

        # Get parent issue for project key
        parent_issue = await jira.get_issue(ticket_key)
        project_key = parent_issue.project_key

        # Build list of available repos from:
        # 1. Feature ticket labels (repo:owner/repo-name)
        # 2. forge.repos Jira project property (required)
        feature_labels = await jira.get_labels(ticket_key)

        available_repos = set()

        # Add repos from Feature labels
        for label in feature_labels:
            if label.startswith("repo:"):
                available_repos.add(label[5:])

        # Add repos from Jira project property (required in strict mode)
        settings = get_settings()
        try:
            for repo in await jira.get_project_repos(project_key):
                available_repos.add(repo)
        except MissingProjectConfig as e:
            if settings.forge_require_project_config:
                logger.error(
                    f"Project {project_key}: {e} — posting config instructions and blocking"
                )
                await jira.add_comment(ticket_key, _missing_repo_config_comment(project_key))
                await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
                return {**state, "last_error": str(e), "current_node": "decompose_epics"}
            logger.warning(f"Project {project_key}: {e} — falling back to GITHUB_KNOWN_REPOS")
            for repo in settings.known_repos:
                available_repos.add(repo)

        available_repos = list(available_repos)

        # Build context for Epic generation
        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "ticket_type": state.get("ticket_type", ""),
            "current_node": state.get("current_node", ""),
            "event_type": state.get("event_type", ""),
            "event_source": state.get("context", {}).get("source", ""),
            "retry_count": state.get("retry_count", 0),
            "project_key": project_key,
            "feature_summary": parent_issue.summary,
            "available_repos": available_repos,
            "feedback": state.get("feedback_comment", ""),
        }

        # Generate Epic breakdown using Claude - primary operation
        epics_data = await agent.generate_epics(spec_content, context)

        if not epics_data:
            logger.warning(f"No Epics generated for {ticket_key}")
            return {
                **state,
                "last_error": "Epic generation returned no results",
                "current_node": "decompose_epics",
            }

        # Create Epics in Jira - secondary operation
        epics_by_repo: dict[str, list[str]] = {}

        for epic in epics_data:
            summary = epic.get("summary", "Untitled Epic")
            plan = epic.get("plan", "")
            repo = epic.get("repo", "")

            # Build labels for the Epic
            # Include forge:managed for webhook routing and forge:parent for lookup
            labels = [
                ForgeLabel.FORGE_MANAGED.value,
                f"forge:parent:{ticket_key}",
            ]
            if repo and "/" in repo:
                labels.append(f"repo:{repo}")
                # Track which epics go to which repo
                if repo not in epics_by_repo:
                    epics_by_repo[repo] = []

            try:
                epic_key = await jira.create_epic(
                    project_key=project_key,
                    summary=summary,
                    description=plan,
                    parent_key=ticket_key,
                    labels=labels,
                )
                epic_keys.append(epic_key)

                if repo:
                    epics_by_repo[repo].append(epic_key)

                logger.info(
                    f"Created Epic {epic_key}: {summary}" + (f" (repo: {repo})" if repo else "")
                )
            except Exception as e:
                # Log but continue creating remaining Epics
                jira_error = str(e)
                logger.warning(f"Failed to create Epic '{summary}' for {ticket_key}: {e}")

        logger.info(f"Created {len(epic_keys)} Epics for {ticket_key}")

        # If we created some Epics, advance even with partial failures
        if epic_keys:
            # Only set workflow label after confirming epics were created
            try:
                await jira.set_workflow_label(ticket_key, ForgeLabel.PLAN_PENDING)
            except Exception as e:
                jira_error = str(e)
                logger.warning(f"Failed to set workflow label for {ticket_key}: {e}")

            # Store plan summary in generation_context so Q&A can reference it
            generation_context = state.get("generation_context", {})
            plan_summary_parts = []
            for epic in epics_data:
                summary = epic.get("summary", "")
                plan = epic.get("plan", "")
                repo = epic.get("repo", "")
                plan_summary_parts.append(
                    f"## {summary}" + (f" (repo: {repo})" if repo else "") + f"\n{plan}"
                )
            generation_context["plan"] = "\n\n".join(plan_summary_parts)

            return update_state_timestamp(
                {
                    **state,
                    "epic_keys": epic_keys,
                    "generation_context": generation_context,
                    "feedback_comment": None,
                    "revision_requested": False,
                    "current_epic_key": None,
                    "current_node": "plan_approval_gate",
                    "last_error": f"Partial Jira failure: {jira_error}" if jira_error else None,
                }
            )
        else:
            # No Epics created at all - this is a failure
            return {
                **state,
                "last_error": jira_error or "Failed to create any Epics in Jira",
                "current_node": "decompose_epics",
                "retry_count": state.get("retry_count", 0) + 1,
            }

    except Exception as e:
        logger.error(f"Epic decomposition failed for {ticket_key}: {e}")
        # Post error notification to Jira
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "decompose_epics")
        # Save any Epics we managed to create
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "decompose_epics",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if epic_keys:
            result_state["epic_keys"] = epic_keys
        return result_state
    finally:
        await jira.close()
        await agent.close()


async def regenerate_all_epics(state: WorkflowState) -> WorkflowState:
    """Delete all Epics and regenerate from spec with feedback.

    This handles Feature-level rejection where the entire Epic
    breakdown needs to be revised.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new epic_keys.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    existing_epics = state.get("epic_keys", [])

    logger.info(f"Regenerating all Epics for {ticket_key} with feedback")

    jira = JiraClient()

    try:
        # Archive existing Epics (unlink from parent, mark as archived)
        for epic_key in existing_epics:
            try:
                await jira.archive_issue(epic_key, archive_subtasks=True)
                logger.info(f"Archived Epic {epic_key}")
            except Exception as e:
                logger.warning(f"Failed to archive Epic {epic_key}: {e}")

        # Clear epic_keys and set feedback for decomposition
        updated_state = {
            **state,
            "epic_keys": [],
            "feedback_comment": feedback,
        }

        # Re-run decomposition (which will use context including feedback)
        return await decompose_epics(updated_state)

    except Exception as e:
        logger.error(f"Epic regeneration failed for {ticket_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_all_epics",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()


async def update_single_epic(state: WorkflowState) -> WorkflowState:
    """Update a single Epic's implementation plan based on feedback.

    This handles Epic-level feedback where only one Epic needs revision.

    Args:
        state: Workflow state with current_epic_key and feedback_comment.

    Returns:
        Updated state.
    """
    ticket_key = state["ticket_key"]
    epic_key = state.get("current_epic_key")
    feedback = state.get("feedback_comment", "")

    if not epic_key:
        logger.warning(f"No current_epic_key for single Epic update on {ticket_key}")
        return state

    logger.info(f"Updating Epic {epic_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Get current Epic description
        epic_issue = await jira.get_issue(epic_key)
        original_plan = epic_issue.description or ""

        # Regenerate plan with feedback
        new_plan = await agent.regenerate_with_feedback(
            original_content=original_plan,
            feedback=feedback,
            content_type="epic",
            ticket_key=ticket_key,
            context={
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
        )

        # Update Epic description
        await jira.update_description(epic_key, new_plan)

        # Add comment to Epic acknowledging revision
        await jira.add_comment(
            epic_key,
            "Implementation plan has been revised based on feedback.",
        )

        logger.info(f"Updated Epic {epic_key} plan")

        return update_state_timestamp(
            {
                **state,
                "current_epic_key": None,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "plan_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Epic update failed for {epic_key}: {e}")
        return {
            **state,
            "last_error": str(e),
            "current_node": "update_single_epic",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()


def check_all_epics_approved(state: WorkflowState, epic_statuses: dict[str, str]) -> bool:
    """Check if all Epics have been approved.

    Args:
        state: Current workflow state.
        epic_statuses: Dict mapping Epic key to current status.

    Returns:
        True if all Epics are approved.
    """
    epic_keys = state.get("epic_keys", [])
    if not epic_keys:
        return False

    approved_status = "approved"  # Adjust based on actual Jira workflow

    for epic_key in epic_keys:
        status = epic_statuses.get(epic_key, "").lower()
        if approved_status not in status:
            return False

    return True
