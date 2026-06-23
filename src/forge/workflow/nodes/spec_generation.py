"""Specification generation node for LangGraph workflow."""

import logging
from datetime import UTC, datetime
from typing import Any

from forge.config import get_settings
from forge.integrations.agents import ForgeAgent
from forge.integrations.jira.client import JiraClient
from forge.models.workflow import ForgeLabel
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.utils import update_state_timestamp
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.qa_summary import post_qa_summary_if_needed

logger = logging.getLogger(__name__)


async def generate_spec(state: WorkflowState) -> WorkflowState:
    """Generate a behavioral specification from the approved PRD.

    This node:
    1. Reads the PRD content from state (or fetches from Jira)
    2. Generates a specification with Given/When/Then acceptance criteria
    3. Stores spec in Jira custom field
    4. Transitions ticket to "Pending Spec Approval"

    Args:
        state: Current workflow state with prd_content.

    Returns:
        Updated state with spec_content populated.
    """
    ticket_key = state["ticket_key"]
    prd_content = state.get("prd_content", "")

    logger.info(f"Generating specification for {ticket_key}")

    # Post Q&A summary for PRD if any
    qa_history = state.get("qa_history", [])
    if qa_history:
        await post_qa_summary_if_needed(ticket_key, qa_history, "prd")

    jira = JiraClient()
    agent = ForgeAgent()
    spec_content = None
    jira_error = None

    try:
        await post_status_comment(
            jira,
            ticket_key,
            "📋 Forge is generating your specification — this may take a few minutes.",
        )

        # If PRD not in state, fetch from Jira
        if not prd_content:
            issue = await jira.get_issue(ticket_key)
            prd_content = issue.description or ""

        if not prd_content.strip():
            logger.warning(f"No PRD content found for {ticket_key}")
            return {
                **state,
                "last_error": "No PRD content available for spec generation",
                "current_node": "generate_spec",
            }

        # Build context
        context: dict[str, Any] = {
            "ticket_key": ticket_key,
            "ticket_type": state.get("ticket_type", ""),
            "current_node": state.get("current_node", ""),
            "event_type": state.get("event_type", ""),
            "event_source": state.get("context", {}).get("source", ""),
            "retry_count": state.get("retry_count", 0),
        }

        # Generate specification using Claude - primary operation
        spec_content = await agent.generate_spec(prd_content, context)

        # Store spec in Jira - secondary operation
        try:
            settings = get_settings()
            if settings.jira_store_in_comments:
                await jira.add_structured_comment(
                    ticket_key,
                    "Technical Specification",
                    spec_content,
                    comment_type="spec",
                )
            elif settings.jira_spec_custom_field:
                await jira.update_custom_field(
                    ticket_key,
                    settings.jira_spec_custom_field,
                    spec_content,
                )
            else:
                # Default: store as markdown attachment
                await jira.add_attachment(
                    ticket_key,
                    filename=f"{ticket_key}-spec.md",
                    content=spec_content,
                    content_type="text/markdown",
                )

            # Set workflow label (instead of custom status transition)
            await jira.set_workflow_label(ticket_key, ForgeLabel.SPEC_PENDING)
        except Exception as e:
            # Jira update failed but we have content - log and continue
            jira_error = str(e)
            logger.warning(f"Jira update failed for {ticket_key}, but spec was generated: {e}")

        logger.info(f"Spec generated for {ticket_key} ({len(spec_content)} chars)")

        # Store generation context for Q&A mode
        generation_context = state.get("generation_context", {})
        generation_context["spec"] = {
            "prd_content": prd_content,
            "generated_at": datetime.now(UTC).isoformat(),
        }

        return update_state_timestamp(
            {
                **state,
                "spec_content": spec_content,
                "generation_context": generation_context,
                "current_node": "spec_approval_gate",
                "last_error": f"Jira update pending: {jira_error}" if jira_error else None,
            }
        )

    except Exception as e:
        logger.error(f"Spec generation failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "generate_spec")
        # If we have partial content, save it even on failure
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "generate_spec",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if spec_content:
            result_state["spec_content"] = spec_content
        return result_state
    finally:
        await jira.close()
        await agent.close()


async def regenerate_spec_with_feedback(state: WorkflowState) -> WorkflowState:
    """Regenerate specification incorporating user feedback.

    Args:
        state: Current workflow state with feedback_comment set.

    Returns:
        Updated state with new spec_content.
    """
    ticket_key = state["ticket_key"]
    feedback = state.get("feedback_comment", "")
    original_spec = state.get("spec_content", "")

    if not feedback:
        logger.warning(f"No feedback provided for spec regeneration on {ticket_key}")
        return state

    logger.info(f"Regenerating spec for {ticket_key} with feedback")

    jira = JiraClient()
    agent = ForgeAgent()

    try:
        # Regenerate spec with feedback
        new_spec = await agent.regenerate_with_feedback(
            original_content=original_spec,
            feedback=feedback,
            content_type="spec",
            ticket_key=ticket_key,
            context={
                "ticket_type": state.get("ticket_type", ""),
                "current_node": state.get("current_node", ""),
                "event_type": state.get("event_type", ""),
                "event_source": state.get("context", {}).get("source", ""),
                "retry_count": state.get("retry_count", 0),
            },
        )

        # Store updated spec in Jira (comment or custom field based on config)
        settings = get_settings()
        if settings.jira_store_in_comments:
            await jira.add_structured_comment(
                ticket_key,
                "Technical Specification (Revised)",
                new_spec,
                comment_type="spec",
            )
        elif settings.jira_spec_custom_field:
            await jira.update_custom_field(
                ticket_key,
                settings.jira_spec_custom_field,
                new_spec,
            )
        else:
            # Default: replace attachment - delete old one first, then add new
            old_filename = f"{ticket_key}-spec.md"
            deleted = await jira.delete_attachments_by_name(ticket_key, old_filename)
            if deleted:
                logger.info(f"Deleted {deleted} old spec attachment(s) for {ticket_key}")

            await jira.add_attachment(
                ticket_key,
                filename=old_filename,
                content=new_spec,
                content_type="text/markdown",
            )

        # Add comment acknowledging revision
        await jira.add_comment(
            ticket_key,
            "📋 Specification has been revised based on feedback. Please review the updated version.",
        )

        logger.info(f"Spec regenerated for {ticket_key} ({len(new_spec)} chars)")

        return update_state_timestamp(
            {
                **state,
                "spec_content": new_spec,
                "feedback_comment": None,
                "revision_requested": False,
                "current_node": "spec_approval_gate",
                "last_error": None,
            }
        )

    except Exception as e:
        logger.error(f"Spec regeneration failed for {ticket_key}: {e}")
        from forge.workflow.nodes.error_handler import notify_error

        await notify_error(state, str(e), "regenerate_spec")
        return {
            **state,
            "last_error": str(e),
            "current_node": "regenerate_spec",
            "retry_count": state.get("retry_count", 0) + 1,
        }
    finally:
        await jira.close()
        await agent.close()
