"""Epic decomposition node for LangGraph workflow."""

import logging
from datetime import UTC, datetime
from typing import Any, cast

from forge.config import get_settings
from forge.integrations.jira.client import MissingProjectConfig
from forge.models.draft import DraftItem, ForgeDecompositionDraft
from forge.models.workflow import ForgeLabel
from forge.workflow.effect_runtime import JiraClient
from forge.workflow.feature.state import FeatureState as WorkflowState
from forge.workflow.projections.artifact_generation import project_artifact_generation
from forge.workflow.stations.artifact_generation import (
    ArtifactKind,
)
from forge.workflow.stations.runner import invoke_builtin_station
from forge.workflow.utils import check_direct_mode, check_yolo_mode, update_state_timestamp
from forge.workflow.utils.draft_manager import DraftManager
from forge.workflow.utils.jira_status import post_status_comment
from forge.workflow.utils.qa_summary import post_qa_summary_if_needed
from forge.workflow.utils.references import fetch_and_inject_references
from forge.workflow.utils.repo_resolution import ensure_repo_labels, get_effective_repos
from forge.workflow.utils.workflow_identity import workflow_identity_labels

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
    2. Generates 2-5 cohesive Epics using the configured LLM backend
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

        available_repos_set: set[str] = set()

        # Add repos from Feature labels
        for label in feature_labels:
            if label.startswith("repo:"):
                available_repos_set.add(label[5:])

        # Add repos from Jira project property (required in strict mode)
        settings = get_settings()
        try:
            for repo in await get_effective_repos(jira, project_key):
                available_repos_set.add(repo)
        except MissingProjectConfig as e:
            if settings.forge_require_project_config:
                logger.error(
                    f"Project {project_key}: {e} — posting config instructions and blocking"
                )
                await post_status_comment(
                    jira, ticket_key, _missing_repo_config_comment(project_key)
                )
                await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
                return {**state, "last_error": str(e), "current_node": "decompose_epics"}
            logger.error(f"Project {project_key}: {e} — posting config instructions and blocking")
            await post_status_comment(
                jira,
                ticket_key,
                "⚠️ Forge local repository configuration is missing.\n\n"
                "Set `GITHUB_KNOWN_REPOS` to a comma-separated list of `owner/repo` values, "
                "then add `forge:retry` to resume.\n\n"
                f"Details: {e}",
            )
            await jira.set_workflow_label(ticket_key, ForgeLabel.BLOCKED)
            return {**state, "last_error": str(e), "current_node": "decompose_epics"}

        available_repos: list[str] = list(available_repos_set)

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

        spec_content_with_refs = await fetch_and_inject_references(state, jira, spec_content)

        # Generate Epic breakdown using the configured LLM backend - primary operation
        outcome = await invoke_builtin_station(
            project_artifact_generation(
                state,
                kind=ArtifactKind.EPICS,
                source_content=spec_content_with_refs,
                context=context,
            )
        )
        assert outcome.output is not None
        epics_data = outcome.output.content
        if not isinstance(epics_data, list):
            raise ValueError("Epic generation station returned a non-list result")

        if not epics_data:
            logger.warning(f"No Epics generated for {ticket_key}")
            return cast(
                WorkflowState,
                {
                    **state,
                    "last_error": "Epic generation returned no results",
                    "current_node": "decompose_epics",
                    "retry_count": state.get("retry_count", 0) + 1,
                },
            )

        await ensure_repo_labels(
            jira,
            parent_issue,
            spec_content_with_refs,
            [str(epic.get("repo", "")) for epic in epics_data],
        )

        # Check parent Jira ticket labels to check for forge:yolo and inspect global config yolo_mode
        is_yolo = check_yolo_mode(state, feature_labels)
        is_direct = check_direct_mode(state, feature_labels)

        if is_yolo or is_direct:
            # Create Epics in Jira immediately
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
                    *workflow_identity_labels(state),
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

                await jira.add_comment(
                    ticket_key,
                    "## 🤖 Forge interaction options\n\n"
                    f"- ✅ **Approve:** add `{ForgeLabel.PLAN_APPROVED.value}` to continue.\n"
                    "- ♻️ **Revise all epics:** add a comment starting with `!` on this ticket.\n"
                    "- 🔧 **Revise a single epic:** add a comment starting with `!` on the Epic.\n"
                    "- ❓ **Ask a question:** add a Jira comment starting with `?`.\n\n"
                    "### Supported Workflow Modes\n"
                    "1. **Default Draft Review Flow:** Forge stores the draft in workflow state and posts a detailed markdown preview. Users can use `/forge` commands or comment starting with `!` to revise, and approve via `/forge approve` or adding the `forge:plan-approved` label.\n"
                    "2. **Direct Mode (`forge:direct-mode`):** Forge directly creates the Epic issues in Jira, then pauses awaiting human approval (adding `forge:plan-approved` label).\n"
                    "3. **YOLO Mode (`forge:yolo`):** Forge bypasses human approval gates, automatically creating the Epic issues in Jira and auto-advancing without pausing.",
                )

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

                return cast(
                    WorkflowState,
                    update_state_timestamp(
                        {
                            **state,
                            "epic_keys": epic_keys,
                            "generation_context": generation_context,
                            "feedback_comment": None,
                            "revision_requested": False,
                            "current_epic_key": None,
                            "current_node": "plan_approval_gate",
                            "is_paused": not is_yolo,
                            "last_error": f"Partial Jira failure: {jira_error}"
                            if jira_error
                            else None,
                        }
                    ),
                )
            else:
                # No Epics created at all - this is a failure
                return cast(
                    WorkflowState,
                    {
                        **state,
                        "last_error": jira_error or "Failed to create any Epics in Jira",
                        "current_node": "decompose_epics",
                        "retry_count": state.get("retry_count", 0) + 1,
                    },
                )
        else:
            # Draft Review Flow (YOLO is inactive)
            # Empty-draft guard to prevent proceeding without draft epics
            if not epics_data:
                return cast(
                    WorkflowState,
                    {
                        **state,
                        "last_error": "Failed to generate any draft Epics",
                        "current_node": "decompose_epics",
                        "retry_count": state.get("retry_count", 0) + 1,
                    },
                )

            # Convert epics_data into DraftItem instances
            draft_items = []
            for idx, epic in enumerate(epics_data, start=1):
                summary = epic.get("summary", "Untitled Epic")
                plan = epic.get("plan", "")
                repo = epic.get("repo", "")
                draft_items.append(
                    DraftItem(
                        id=idx,
                        summary=summary,
                        description=plan,
                        repo=repo,
                        acceptance_criteria=[],
                        excluded=False,
                    )
                )

            # Create Draft model
            draft = ForgeDecompositionDraft(
                parent_key=ticket_key,
                phase="epics",
                items=draft_items,
                version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

            # Format Markdown review comment outlining proposed items
            # Implement BR-003 Truncation Boundary
            comment_body = DraftManager.format_review_comment(draft)

            # Post the review comment to the parent Jira ticket
            await jira.add_comment(ticket_key, comment_body)

            # Set workflow label to pending
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

            # Transition state to pause the workflow at the plan_approval_gate (setting is_paused = True and appropriate workflow flags)
            return cast(
                WorkflowState,
                update_state_timestamp(
                    {
                        **state,
                        "plan_draft": draft,
                        "epic_keys": [],
                        "generation_context": generation_context,
                        "feedback_comment": None,
                        "revision_requested": False,
                        "current_epic_key": None,
                        "current_node": "plan_approval_gate",
                        "is_paused": True,
                        "last_error": f"Partial Jira failure: {jira_error}" if jira_error else None,
                    }
                ),
            )

    except Exception as e:
        logger.error(f"Epic decomposition failed for {ticket_key}: {e}")
        # Save any Epics we managed to create
        result_state = {
            **state,
            "last_error": str(e),
            "current_node": "decompose_epics",
            "retry_count": state.get("retry_count", 0) + 1,
        }
        if epic_keys:
            result_state["epic_keys"] = epic_keys
        return cast(WorkflowState, result_state)
    finally:
        await jira.close()


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
        return await decompose_epics(cast(WorkflowState, updated_state))

    except Exception as e:
        logger.error(f"Epic regeneration failed for {ticket_key}: {e}")
        return cast(
            WorkflowState,
            {
                **state,
                "last_error": str(e),
                "current_node": "regenerate_all_epics",
                "retry_count": state.get("retry_count", 0) + 1,
            },
        )
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
    feedback = state.get("feedback_comment") or ""

    if not epic_key:
        logger.warning(f"No current_epic_key for single Epic update on {ticket_key}")
        return state

    logger.info(f"Updating Epic {epic_key} with feedback")

    jira = JiraClient()
    try:
        # Get current Epic description
        epic_issue = await jira.get_issue(epic_key)
        original_plan = epic_issue.description or ""

        original_plan_with_refs = await fetch_and_inject_references(state, jira, original_plan)

        # Regenerate plan with feedback
        outcome = await invoke_builtin_station(
            project_artifact_generation(
                state,
                kind=ArtifactKind.EPICS,
                source_content=original_plan_with_refs,
                feedback=feedback,
                context={
                    "ticket_type": state.get("ticket_type", ""),
                    "current_node": state.get("current_node", ""),
                    "event_type": state.get("event_type", ""),
                    "event_source": state.get("context", {}).get("source", ""),
                    "retry_count": state.get("retry_count", 0),
                },
            )
        )
        assert outcome.output is not None
        new_plan = str(outcome.output.content)

        # Update Epic description
        await jira.update_description(epic_key, new_plan)

        # Add comment to Epic acknowledging revision
        await post_status_comment(
            jira,
            epic_key,
            "Implementation plan has been revised based on feedback.",
        )

        logger.info(f"Updated Epic {epic_key} plan")

        return cast(
            WorkflowState,
            update_state_timestamp(
                {
                    **state,
                    "current_epic_key": None,
                    "feedback_comment": None,
                    "revision_requested": False,
                    "current_node": "plan_approval_gate",
                    "last_error": None,
                }
            ),
        )

    except Exception as e:
        logger.error(f"Epic update failed for {epic_key}: {e}")
        return cast(
            WorkflowState,
            {
                **state,
                "last_error": str(e),
                "current_node": "update_single_epic",
                "retry_count": state.get("retry_count", 0) + 1,
            },
        )
    finally:
        await jira.close()


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
