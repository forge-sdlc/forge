"""PRD generation node for LangGraph workflow."""

import logging
from datetime import UTC, datetime
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp

logger = logging.getLogger(__name__)


async def generate_prd(state: WorkflowState) -> WorkflowState:
    """Generate a PRD from raw requirements in Jira description.

    This node:
    1. Reads the current Jira issue description
    2. Generates a structured PRD using Claude
    3. Updates the Jira description with the PRD
    4. Transitions the ticket to "Pending PRD Approval"

    Args:
        state: Current workflow state.

    Returns:
        Updated state with prd_content populated.
    """
    ticket_key = state["ticket_key"]
    logger.info(f"Generating PRD for {ticket_key}")

    jira = JiraClient()
    agent = ForgeAgent()
    prd_content = None
    jira_error = None

    try:
        # Fetch current issue to get raw requirements
        issue = await jira.get_issue(ticket_key)
        raw_requirements = issue.description or ""

        if not raw_requirements.strip():
            logger.warning(f"No description found for {ticket_key}")
            return {
                **state,
                "last_error": "No requirements found in issue description",
                "current_node": "generate_prd",
            }

        # Build context from issue metadata
        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "summary": issue.summary,
            "project_key": issue.project_key,
        }

        # Generate PRD using Claude - primary operation
        prd_content = await agent.generate_prd(raw_requirements, context)

        # Update Jira with generated PRD - secondary operation
        try:
            settings = get_settings()
            if settings.jira_store_in_comments:
                # Store PRD in a structured comment
                await jira.add_structured_comment(
                    ticket_key,
                    "Product Requirements Document (PRD)",
                    prd_content,
                    comment_type="prd",
                )
            else:
                # Update description directly
                await jira.update_description(ticket_key, prd_content)

            # Set workflow label (instead of custom status transition)
            await jira.set_workflow_label(ticket_key, ForgeLabel.PRD_PENDING)
        except Exception as e:
            # Jira update failed but we have content - log and continue
            jira_error = str(e)
            logger.warning(f"Jira update failed for {ticket_key}, but PRD was generated: {e}")

        logger.info(f"PRD generated for {ticket_key} ({len(prd_content)} chars)")

        # Store generation context for Q&A mode
        generation_context = state.get("generation_context", {})
        generation_context["prd"] = {
            "raw_requirements": raw_requirements,
            "summary": issue.summary,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        # If Jira failed, set a warning but still advance (content exists)
        return update_state_timestamp(
            {
                **state,
                "prd_content": prd_content,
                "generation_context": generation_context,
                "current_node": "prd_approval_gate",
                "last_error": f"Jira update pending: {jira_error}" if jira_error else None,
            }
        )

    except Exception as e:
        logger.error(f"PRD generation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "generate_prd")
        # If we have partial content, save it even on failure
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "generate_prd",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if prd_content:
            result_state["prd_content"] = prd_content
        return result_state
    finally:
        await jira.close()
        await agent.close()


async def regenerate_prd_with_feedback(state: WorkflowState) -> WorkflowState:
    """Regenerate PRD incorporating user feedback.

    This node handles the case where a PM rejects the PRD and provides
    feedback via a Jira comment. It regenerates the PRD addressing
    the feedback and updates Jira.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new prd_content.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    original_prd = state.get("prd_content", "")

    if not feedback:
        logger.warning(f"No feedback provided for PRD regeneration on {ticket_key}")
        return state

    logger.info(f"Regenerating PRD for {ticket_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Regenerate PRD with feedback
        new_prd = await agent.regenerate_with_feedback(
            original_content=original_prd,
            feedback=feedback,
            content_type="prd",
            ticket_key=ticket_key,
        )

        # Update Jira with regenerated PRD
        settings = get_settings()
        if settings.jira_store_in_comments:
            await jira.add_structured_comment(
                ticket_key,
                "Product Requirements Document (PRD)",
                new_prd,
                comment_type="prd",
            )
        else:
            await jira.update_description(ticket_key, new_prd)

        # Add comment acknowledging the revision
        await jira.add_comment(
            ticket_key,
            "PRD has been revised based on feedback. Please review.",
        )

        logger.info(f"PRD regenerated for {ticket_key} ({len(new_prd)} chars)")

        return update_state_timestamp(
            {
                **state,
                "prd_content": new_prd,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "prd_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"PRD regeneration failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_prd")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_prd",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()
