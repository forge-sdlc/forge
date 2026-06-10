"""Orchestrator worker that consumes events from Redis and processes them."""

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import uuid
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from forge.api.routes.metrics import (
    record_workflow_completed,
    record_workflow_failed,
    record_workflow_started,
)
from forge.config import get_settings
from forge.integrations.github.client import GitHubClient
from forge.integrations.jira.client import JiraClient
from forge.models.events import EventSource
from forge.models.workflow import TicketType
from forge.orchestrator.checkpointer import get_checkpointer, get_ticket_from_pr_index
from forge.queue.consumer import QueueConsumer
from forge.queue.models import QueueMessage
from forge.skills.orchestrator import ensure_skills
from forge.skills.utils import extract_project_key
from forge.workflow.registry import create_default_router
from forge.workflow.router import WorkflowRouter
from forge.workflow.utils.comment_classifier import CommentType, classify_comment

logger = logging.getLogger(__name__)


def _is_workflow_errored(state: dict) -> bool:
    """Return True when workflow has a recorded error and is not paused for human input."""
    return not state.get("is_paused") and state.get("last_error") is not None


# Matches >option N anywhere in comment (case-insensitive, first match wins)
# Supports both start-of-line usage (>option 2) and in-prose usage (let's go with >option 2)
_OPTION_PATTERN = re.compile(r"(?mi)>option\s+(\d+)")


class OrchestratorWorker:
    """Worker that processes workflow events from Redis queue."""

    def __init__(
        self,
        consumer_name: str | None = None,
        router: WorkflowRouter | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            consumer_name: Unique name for this consumer. Auto-generated if not provided.
            router: WorkflowRouter for selecting workflows. Uses default if not provided.
        """
        self.settings = get_settings()
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self.consumer = QueueConsumer(self.consumer_name)
        self.router = router or create_default_router()
        self._shutdown_event = asyncio.Event()
        self._checkpointer = None
        self._compiled_workflows: dict[str, Any] = {}  # Cache compiled workflows by name

    async def _handle_jira_event(self, message: QueueMessage) -> None:
        """Handle a Jira webhook event.

        Args:
            message: The queue message to process.
        """
        await self._process_workflow(message)

    async def _handle_github_event(self, message: QueueMessage) -> None:
        """Handle a GitHub webhook event.

        Args:
            message: The queue message to process.
        """
        if not message.ticket_key:
            message = await self._resolve_ticket_from_pr_index(message)
            if not message.ticket_key:
                logger.info(
                    f"Dropping GitHub event {message.event_id}: "
                    "no ticket key in message and PR URL not found in Redis index"
                )
                return
        await self._process_workflow(message)

    async def _resolve_ticket_from_pr_index(self, message: QueueMessage) -> QueueMessage:
        """Attempt to resolve ticket key from Redis PR index when not in message.

        Extracts the PR URL from the event payload and looks it up in the
        forge:pr_index Redis key populated at PR creation time.

        Args:
            message: Queue message with empty ticket_key.

        Returns:
            Message with ticket_key populated if found, otherwise unchanged.
        """
        payload = message.payload
        repo = payload.get("repository", {}).get("full_name", "")
        api_url = payload.get("review", {}).get("pull_request_url", "")
        suite_prs = (
            payload.get("check_suite", {}).get("pull_requests")
            or payload.get("check_run", {}).get("pull_requests")
            or []
        )
        pr_number = (
            payload.get("pull_request", {}).get("number")
            or payload.get("issue", {}).get("number")
            or (suite_prs[0].get("number") if suite_prs else None)
        )

        pr_url = (
            payload.get("pull_request", {}).get("html_url")
            or payload.get("review", {}).get("html_url")
            or (f"https://github.com/{repo}/pull/{pr_number}" if repo and pr_number else None)
            or (
                api_url.replace("https://api.github.com/repos/", "https://github.com/").replace(
                    "/pulls/", "/pull/"
                )
                if api_url
                else None
            )
        )

        logger.debug(f"PR URL extracted for {message.event_id}: {pr_url!r}")

        if not pr_url:
            return message

        try:
            ticket_key = await get_ticket_from_pr_index(pr_url)
            if ticket_key:
                logger.info(
                    f"Resolved ticket key {ticket_key} for GitHub event "
                    f"{message.event_id} from PR index ({pr_url})"
                )
                return dataclass_replace(message, ticket_key=ticket_key)
        except Exception:
            logger.warning(
                f"PR index lookup failed for {pr_url}",
                exc_info=True,
            )

        return message

    async def _process_workflow(self, message: QueueMessage) -> None:
        """Process a message through the workflow.

        Args:
            message: The queue message to process.
        """
        ticket_key = message.ticket_key
        logger.info(f"Processing {message.source.value} event for {ticket_key}")

        # Synchronise skills before any workflow resolution so that both new
        # and resumed workflows always start with up-to-date skill packages.
        try:
            project_key = extract_project_key(ticket_key)
            jira_client = JiraClient()
            skills_dir = Path(self.settings.skills_dir)
            await ensure_skills(project_key, jira_client, skills_dir)
        except Exception:
            logger.warning(
                "Skill synchronisation failed for %s; continuing with workflow.",
                ticket_key,
                exc_info=True,
            )

        try:
            # Determine ticket type early to select workflow
            ticket_type = self._extract_ticket_type(message)

            workflow_instance = None
            existing_state = None
            config = {"configurable": {"thread_id": ticket_key}}

            if ticket_type == TicketType.UNKNOWN:
                # GitHub events (and other non-Jira sources) don't carry ticket type.
                # Find the workflow by scanning checkpoint state across all registered workflows.
                workflow_instance, existing_state = await self._find_workflow_by_state(ticket_key)
                if workflow_instance is None:
                    logger.warning(
                        f"No existing workflow state found for {ticket_key} "
                        f"({message.source.value} event with unknown ticket type). Skipping."
                    )
                    return
                # Recover ticket type from checkpointed state so metrics are accurate.
                if existing_state and existing_state.values:
                    stored_type = existing_state.values.get("ticket_type", "Unknown")
                    with contextlib.suppress(ValueError):
                        ticket_type = TicketType(stored_type)
                logger.info(
                    f"Resolved workflow for {ticket_key} from checkpoint state "
                    f"(type={ticket_type}, workflow={workflow_instance.name})"
                )
            else:
                # Use router to resolve which workflow to use
                workflow_instance = self.router.resolve(
                    ticket_type=ticket_type,
                    labels=[],  # TODO: Extract labels from message payload
                    event=message.payload,
                )

                if workflow_instance is None:
                    logger.error(
                        f"No workflow found for ticket {ticket_key} (type={ticket_type}). Skipping."
                    )
                    return

            # Get or compile the workflow graph
            compiled_workflow = self._get_compiled_workflow(workflow_instance)

            # Fetch existing state if not already loaded (non-GitHub path)
            if existing_state is None:
                existing_state = await compiled_workflow.aget_state(config)

            # Debug logging for checkpoint state
            logger.debug(f"Existing state for {ticket_key}: {existing_state}")
            if existing_state:
                logger.debug(f"State values: {existing_state.values}")
                logger.debug(
                    f"is_paused: {existing_state.values.get('is_paused') if existing_state.values else None}"
                )

            # Check if we should resume an existing workflow
            should_resume = False
            if existing_state and existing_state.values:
                values = existing_state.values
                current_node = values.get("current_node", "")
                is_paused = values.get("is_paused", False)
                has_error = values.get("last_error") is not None

                # Resume if: explicitly paused, or has a node state (not at start/end)
                if is_paused:
                    should_resume = True
                    logger.info(f"Workflow is paused at {current_node}")
                elif current_node and current_node not in ("entry", "__end__", ""):
                    # Workflow has progress - resume from current state
                    should_resume = True
                    if has_error:
                        logger.info(f"Workflow has error at {current_node}, resuming")
                    else:
                        logger.info(f"Workflow in progress at {current_node}, resuming")

            if should_resume:
                # Resume workflow - check for approval/rejection signals
                updated_values = await self._handle_resume_event(message, existing_state.values)

                # _handle_resume_event returns early (unchanged current_node) when
                # the workflow is at a terminal state without an explicit retry signal.
                # In that case just persist the state update and stop.
                # and stop — don't try to invoke a finished graph.
                terminal_nodes = ("complete", "complete_tasks", "aggregate_feature_status")
                is_terminal_or_blocked = updated_values.get(
                    "current_node"
                ) in terminal_nodes or updated_values.get("is_blocked", False)
                if is_terminal_or_blocked:
                    state_desc = (
                        "terminal"
                        if updated_values.get("current_node") in terminal_nodes
                        else "blocked"
                    )
                    logger.info(
                        f"Workflow for {ticket_key} at {state_desc} state "
                        f"'{updated_values.get('current_node')}', skipping invocation"
                    )
                    await compiled_workflow.aupdate_state(config, updated_values)
                    return

                # If _handle_resume_event returned the state object unchanged (identity
                # check), no signal was recognised — do not invoke the workflow.
                # Without this guard, nodes in needs_fresh_invoke (e.g. human_review_gate)
                # would be re-invoked with is_paused=True and immediately re-pause,
                # producing a misleading "Resuming workflow" log with no real effect.
                if updated_values is existing_state.values:
                    return

                logger.info(f"Resuming workflow for {ticket_key}")

                was_errored = _is_workflow_errored(existing_state.values)

                # Nodes that wait for external events (CI webhooks, human review)
                # must be re-invoked fresh so route_by_ticket_type re-runs them.
                # ainvoke(None) only replays the routing edge after the node, not
                # the node itself, so CI status would never be re-checked.
                needs_fresh_invoke = updated_values.get("current_node") in (
                    "ci_evaluator",
                    "attempt_ci_fix",
                    "human_review_gate",
                    "rebase_pr",
                )

                if was_errored or needs_fresh_invoke:
                    logger.info(
                        f"{'Retrying' if was_errored else 'Re-invoking'} workflow "
                        f"from {updated_values.get('current_node')}"
                    )
                    result = await compiled_workflow.ainvoke(updated_values, config=config)
                else:
                    # For normal resume (paused at approval gate): update state and continue
                    await compiled_workflow.aupdate_state(config, updated_values)
                    result = await compiled_workflow.ainvoke(None, config=config)
            else:
                # New workflow - build initial state
                state = self._build_initial_state(message)
                logger.info(f"Starting new workflow for {ticket_key}")

                # Record workflow started metric
                ticket_type_str = state.get("ticket_type", "unknown")
                record_workflow_started(ticket_type=ticket_type_str)

                # Run the workflow from the beginning
                result = await compiled_workflow.ainvoke(state, config=config)

            final_node = result.get("current_node", "unknown")
            is_paused = result.get("is_paused", False)
            logger.info(
                f"Workflow completed for {ticket_key}, "
                f"final node: {final_node}, "
                f"paused: {is_paused}"
            )

            # Record workflow completed metric (only if not paused - paused means waiting for approval)
            if not is_paused:
                ticket_type = result.get("ticket_type", "unknown")
                record_workflow_completed(ticket_type=ticket_type, final_node=final_node)

        except Exception as e:
            import traceback

            logger.error(f"Workflow failed for {ticket_key}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Record workflow failed metric
            record_workflow_failed(ticket_type="unknown", error_type=type(e).__name__)
            raise  # Let consumer handle retry logic

    async def _handle_resume_event(
        self, message: QueueMessage, current_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle a resume event for a paused workflow.

        Detects approval/rejection signals from the webhook payload.

        Args:
            message: The queue message.
            current_state: Current workflow state from checkpoint.

        Returns:
            Updated state for workflow resumption.
        """
        payload = message.payload
        changelog = payload.get("changelog", {})
        comment = payload.get("comment", {})

        # Check for label changes indicating approval or retry
        label_changes = [
            item for item in changelog.get("items", []) if item.get("field") == "labels"
        ]

        is_approved = False
        is_rejected = False
        is_retry = False
        is_question = False
        is_ci_webhook = False
        pr_merged = False
        feedback = None

        current_node = current_state.get("current_node", "")

        # GitHub check_run/check_suite events are the explicit signal for wait_for_ci_gate.
        # They don't carry Jira labels or comments, so handle them before the label loop.
        # For check_suite and check_run events (both real GitHub webhooks and poller
        # forwarded ones), only wake up CI evaluation when the suite is completed.
        # GitHub fires check_suite webhooks for created/in_progress/completed — evaluating
        # on the earlier actions would see a partial set of check runs and could
        # prematurely declare success. Other event types (push, pull_request) always wake up.
        if (
            current_node in ("wait_for_ci_gate", "ci_evaluator")
            and message.source == EventSource.GITHUB
        ):
            event = message.event_type
            is_check_event = "check_suite" in event or "check_run" in event
            if is_check_event:
                suite_status = payload.get("check_suite", {}).get("status") or payload.get(
                    "check_run", {}
                ).get("check_suite", {}).get("status")
                if suite_status and suite_status != "completed":
                    logger.info(
                        f"Ignoring {event} for {message.ticket_key}: "
                        f"check_suite not yet completed (status={suite_status!r})"
                    )
                else:
                    is_ci_webhook = True
                    logger.info(f"Detected GitHub CI webhook signal for {current_node}")
            elif "issue_comment" not in event:
                is_ci_webhook = True
                logger.info(f"Detected GitHub CI webhook signal for {current_node}")

        # GitHub issue_comment events: detect /forge skip-gate and /forge unskip-gate
        # commands posted as PR comments.
        _CI_STAGES = ("wait_for_ci_gate", "ci_evaluator", "attempt_ci_fix")
        if message.source == EventSource.GITHUB and "issue_comment" in message.event_type:
            gh_comment_body = payload.get("comment", {}).get("body", "").strip()
            repo_full = payload.get("repository", {}).get("full_name", "")
            pr_number = payload.get("issue", {}).get("number")
            sender = payload.get("sender", {}).get("login", "")
            _owner, _, _repo = repo_full.partition("/")

            skip_prefix = "/forge skip-gate"
            unskip_prefix = "/forge unskip-gate"

            if gh_comment_body.lower().startswith(skip_prefix.lower()):
                check_name = gh_comment_body[len(skip_prefix) :].strip()
                if current_node in _CI_STAGES and check_name:
                    skipped = list(current_state.get("ci_skipped_checks", []))
                    if check_name not in skipped:
                        skipped.append(check_name)
                    logger.info(f"CI gate skip added for {message.ticket_key}: '{check_name}'")
                    await self._post_skip_gate_feedback(
                        ticket_key=message.ticket_key,
                        owner=_owner,
                        repo=_repo,
                        pr_number=pr_number,
                        check_name=check_name,
                        sender=sender,
                        action="skip",
                    )
                    return {
                        **current_state,
                        "ci_skipped_checks": skipped,
                        "is_paused": False,
                        "current_node": "ci_evaluator",
                    }
                return current_state

            elif gh_comment_body.lower().startswith(unskip_prefix.lower()):
                check_name = gh_comment_body[len(unskip_prefix) :].strip()
                if current_node in _CI_STAGES and check_name:
                    skipped = [
                        s for s in current_state.get("ci_skipped_checks", []) if s != check_name
                    ]
                    logger.info(f"CI gate skip removed for {message.ticket_key}: '{check_name}'")
                    await self._post_skip_gate_feedback(
                        ticket_key=message.ticket_key,
                        owner=_owner,
                        repo=_repo,
                        pr_number=pr_number,
                        check_name=check_name,
                        sender=sender,
                        action="unskip",
                    )
                    return {
                        **current_state,
                        "ci_skipped_checks": skipped,
                        "is_paused": False,
                        "current_node": "ci_evaluator",
                    }
                return current_state

            rebase_prefix = "/forge rebase"
            if gh_comment_body.lower().startswith(rebase_prefix.lower()):
                if not current_state.get("current_pr_number"):
                    logger.warning(
                        f"Ignoring /forge rebase for {message.ticket_key}: no PR in state"
                    )
                    return current_state

                logger.info(f"Detected /forge rebase for {message.ticket_key}")
                await self._post_rebase_feedback(
                    ticket_key=message.ticket_key,
                    owner=_owner,
                    repo=_repo,
                    pr_number=pr_number,
                    sender=sender,
                )
                return {
                    **current_state,
                    "rebase_return_node": current_node,
                    "is_paused": False,
                    "current_node": "rebase_pr",
                }

        for change in label_changes:
            to_labels = change.get("toString", "")
            from_labels = change.get("fromString", "")

            # Check for retry label - triggers retry of current stage
            if "forge:retry" in to_labels.lower() and "forge:retry" not in from_labels.lower():
                is_retry = True
                logger.info(f"Detected retry signal via forge:retry label for {current_node}")

            # Check for approval labels - but only if it matches the current stage
            if "approved" in to_labels.lower() and "pending" in from_labels.lower():
                # Validate the approval matches the workflow stage
                approval_stage = None
                if "prd-approved" in to_labels.lower():
                    approval_stage = "prd"
                elif "spec-approved" in to_labels.lower():
                    approval_stage = "spec"
                elif "plan-approved" in to_labels.lower():
                    approval_stage = "plan"
                elif "task-approved" in to_labels.lower():
                    approval_stage = "task"

                # Map current node to expected approval stage
                node_to_stage = {
                    "prd_approval_gate": "prd",
                    "generate_prd": "prd",
                    "regenerate_prd": "prd",
                    "spec_approval_gate": "spec",
                    "generate_spec": "spec",
                    "regenerate_spec": "spec",
                    "plan_approval_gate": "plan",
                    "decompose_epics": "plan",
                    "regenerate_all_epics": "plan",
                    "update_single_epic": "plan",
                    "task_approval_gate": "task",
                    "generate_tasks": "task",
                }
                expected_stage = node_to_stage.get(current_node)

                if approval_stage and expected_stage and approval_stage == expected_stage:
                    is_approved = True
                    logger.info(
                        f"Detected {approval_stage} approval via label change: "
                        f"{from_labels} -> {to_labels}"
                    )
                elif approval_stage:
                    logger.warning(
                        f"Ignoring {approval_stage} approval - workflow at {current_node} "
                        f"(expects {expected_stage})"
                    )

        # Fallback: check current labels on the ticket when changelog-based
        # detection missed the approval (e.g. user changed labels in two steps).
        if not is_approved and not is_rejected and not is_retry:
            current_labels = payload.get("issue", {}).get("fields", {}).get("labels", [])
            current_labels_lower = [lbl.lower() for lbl in current_labels]
            gate_to_approved_label = {
                "prd_approval_gate": "forge:prd-approved",
                "spec_approval_gate": "forge:spec-approved",
                "plan_approval_gate": "forge:plan-approved",
                "task_approval_gate": "forge:task-approved",
            }
            expected_label = gate_to_approved_label.get(current_node)
            if expected_label and expected_label in current_labels_lower:
                is_approved = True
                stage = current_node.replace("_approval_gate", "")
                logger.info(f"Detected {stage} approval via current label: {expected_label}")

        # Check for rejection comment (contains feedback)
        # Determine if comment is on Epic/Task (child) vs Feature (parent)
        # based on current workflow phase
        comment_ticket_key = None
        comment_ticket_type = None  # "epic" or "task"
        if comment:
            comment_body = comment.get("body", "")
            # Extract text from ADF if needed
            if isinstance(comment_body, dict):
                comment_body = self._extract_text_from_adf(comment_body)

            if comment_body.strip():
                # >option N detection for rca_option_gate (runs before general classification)
                if current_node == "rca_option_gate":
                    option_match = _OPTION_PATTERN.search(comment_body)
                    if option_match:
                        n = int(option_match.group(1))
                        rca_options = current_state.get("rca_options", [])
                        if 1 <= n <= len(rca_options):
                            logger.info(f"Detected >option {n} for {message.ticket_key}")
                            return {
                                **current_state,
                                "selected_fix_option": n,
                                "selected_fix_approach": rca_options[n - 1],
                                "is_paused": False,
                                "is_question": False,
                                "revision_requested": False,
                                "feedback_comment": None,
                                "context": {
                                    **current_state.get("context", {}),
                                    "resume_event": message.event_type,
                                    "payload": payload,
                                },
                            }
                        else:
                            max_n = len(rca_options)
                            logger.info(
                                f">option {n} out of range (max {max_n}) for {message.ticket_key}"
                            )
                            jira = JiraClient()
                            try:
                                await jira.add_comment(
                                    message.ticket_key,
                                    f"Please reply with >option N where N is between 1 and {max_n}.",
                                )
                            finally:
                                await jira.close()
                            return current_state

                comment_type = classify_comment(comment_body)

                if comment_type == CommentType.QUESTION:
                    is_question = True
                    feedback = comment_body
                    logger.info(f"Detected question comment: {feedback[:100]}...")
                else:
                    # Treat as feedback for rejection
                    is_rejected = True
                    feedback = comment_body

                # Determine workflow phase from current_node for feedback/questions
                # (skip for approvals since they don't have feedback)
                if feedback:
                    workflow_ticket_key = current_state.get("ticket_key", "")
                    epic_keys = current_state.get("epic_keys", [])
                    task_keys = current_state.get("task_keys", [])

                    # source_ticket_key is set by the Jira webhook handler when a
                    # child ticket (Epic/Task) event is re-routed to the parent Feature.
                    # message.ticket_key will equal workflow_ticket_key in that case,
                    # so we use source_ticket_key to detect the true origin.
                    source_ticket_key = payload.get("source_ticket_key")
                    child_ticket_key = (
                        source_ticket_key
                        if source_ticket_key and source_ticket_key != workflow_ticket_key
                        else (
                            message.ticket_key
                            if message.ticket_key != workflow_ticket_key
                            else None
                        )
                    )

                    # Determine which phase we're in based on current_node
                    plan_phase_nodes = (
                        "plan_approval_gate",
                        "decompose_epics",
                        "regenerate_all_epics",
                        "update_single_epic",
                    )
                    task_phase_nodes = (
                        "task_approval_gate",
                        "generate_tasks",
                        "regenerate_all_tasks",
                        "update_single_task",
                    )

                    if child_ticket_key:
                        # Comment originated from a child ticket - determine type by phase
                        if current_node in plan_phase_nodes:
                            # In plan phase - check if it's an Epic
                            if child_ticket_key in epic_keys:
                                comment_ticket_key = child_ticket_key
                                comment_ticket_type = "epic"
                                logger.info(
                                    f"Detected Epic-level comment on {comment_ticket_key}: "
                                    f"{feedback[:100]}..."
                                )
                            else:
                                logger.info(
                                    f"Detected comment on child ticket {child_ticket_key} "
                                    f"(not in epic_keys): {feedback[:100]}..."
                                )
                        elif current_node in task_phase_nodes:
                            # In task phase - check if it's a Task
                            if child_ticket_key in task_keys:
                                comment_ticket_key = child_ticket_key
                                comment_ticket_type = "task"
                                logger.info(
                                    f"Detected Task-level comment on {comment_ticket_key}: "
                                    f"{feedback[:100]}..."
                                )
                            else:
                                logger.info(
                                    f"Detected comment on child ticket {child_ticket_key} "
                                    f"(not in task_keys): {feedback[:100]}..."
                                )
                        else:
                            # Not in a phase that handles child comments
                            logger.info(
                                f"Detected comment on child ticket {child_ticket_key} "
                                f"at unexpected node {current_node}: {feedback[:100]}..."
                            )
                    else:
                        logger.info(f"Detected Feature-level comment: {feedback[:100]}...")

        # GitHub pull_request_review events — handled when at human_review_gate.
        # A review submission is the primary signal for the human review stage.
        if (
            message.source == EventSource.GITHUB
            and "pull_request_review" in message.event_type
            and current_node == "human_review_gate"
        ):
            review = payload.get("review", {})
            review_state = review.get("state", "").lower()
            review_body = review.get("body", "") or ""

            if review_state == "approved":
                # PR approved — advance to complete_tasks (via route_human_review default)
                is_approved = True
                logger.info(f"Detected PR review approval for {message.ticket_key}")
            elif review_state in ("changes_requested", "commented"):
                # Always fetch inline comments so the agent gets the full picture,
                # regardless of whether a summary body is also present.
                repo_full = payload.get("repository", {}).get("full_name", "")
                pr_number = payload.get("pull_request", {}).get("number")
                inline_comments = []
                if repo_full and pr_number:
                    owner, repo_name = repo_full.split("/", 1)
                    gh = GitHubClient()
                    try:
                        inline_comments = await gh.get_pull_request_review_comments(
                            owner, repo_name, pr_number
                        )
                    finally:
                        await gh.close()

                parts = []
                if review_body.strip():
                    parts.append(review_body.strip())
                if inline_comments:
                    inline_text = "\n\n".join(
                        f"**{c['path']}** (line {c['position']}):\n{c['body']}"
                        for c in inline_comments
                    )
                    parts.append(f"Inline comments:\n{inline_text}")

                if parts:
                    feedback = "\n\n".join(parts)
                    is_rejected = True
                    logger.info(
                        f"Detected PR review ({review_state}) for {message.ticket_key}: "
                        f"body={'yes' if review_body.strip() else 'no'}, "
                        f"inline comments={len(inline_comments)}"
                    )
                else:
                    logger.info(
                        f"Detected PR review ({review_state}) for {message.ticket_key} "
                        f"with no body and no inline comments — ignoring"
                    )
                    return current_state

        # GitHub pull_request:closed + merged — PR was actually merged
        if (
            message.source == EventSource.GITHUB
            and "pull_request" in message.event_type
            and payload.get("pull_request", {}).get("merged") is True
            and current_node == "human_review_gate"
        ):
            is_approved = True
            pr_merged = True
            logger.info(f"Detected PR merge for {message.ticket_key}")

        # Build updated state — do NOT set is_paused=False here.
        # Each branch below sets it explicitly when a valid signal is detected.
        # Unrecognized events (wrong-stage approval, unrelated label changes, etc.)
        # must not unpause the workflow — they return current_state unchanged.
        updated_state = {
            **current_state,
            "context": {
                **current_state.get("context", {}),
                "resume_event": message.event_type,
                "payload": payload,
            },
        }

        was_errored = _is_workflow_errored(current_state)

        # Check if workflow is at a terminal state (complete)
        terminal_states = ("complete", "complete_tasks", "aggregate_feature_status")
        is_terminal = current_node in terminal_states

        if is_retry:
            if is_terminal:
                logger.info(
                    f"Ignoring forge:retry for {message.ticket_key} - workflow already complete"
                )
                await self._post_terminal_error_comment(
                    message.ticket_key,
                    "Workflow is already complete — nothing to retry.",
                )
                return current_state

            prev_error = current_state.get("last_error")
            logger.info(
                f"Retry requested for {message.ticket_key} at {current_node} "
                f"(clearing error: {prev_error[:100] if prev_error else 'none'})"
            )
            updated_state["is_paused"] = False
            updated_state["is_blocked"] = False
            updated_state["last_error"] = None
            updated_state["revision_requested"] = False
            updated_state["feedback_comment"] = None
            updated_state["retry_count"] = 0
            updated_state["ci_fix_attempts"] = 0
            # Keep current_node — workflow resumes from the node that failed
        elif is_ci_webhook:
            # GitHub CI event — unpause the gate and let ci_evaluator check the results
            updated_state["is_paused"] = False
        elif is_approved:
            updated_state["is_paused"] = False
            updated_state["revision_requested"] = False
            updated_state["feedback_comment"] = None
            updated_state["last_error"] = None
            if pr_merged:
                updated_state["pr_merged"] = True
        elif is_question:
            # Unpause so answer_question node runs, it will re-pause after answering
            updated_state["is_paused"] = False
            updated_state["is_question"] = True
            updated_state["feedback_comment"] = feedback
            updated_state["revision_requested"] = False
        elif is_rejected and feedback:
            updated_state["is_paused"] = False
            updated_state["revision_requested"] = True
            updated_state["feedback_comment"] = feedback
            if comment_ticket_key and comment_ticket_type == "epic":
                updated_state["current_epic_key"] = comment_ticket_key
                updated_state["current_task_key"] = None
            elif comment_ticket_key and comment_ticket_type == "task":
                updated_state["current_task_key"] = comment_ticket_key
                updated_state["current_epic_key"] = None
            else:
                updated_state["current_task_key"] = None
                updated_state["current_epic_key"] = None
        elif was_errored:
            # Workflow has an error — auto-resume up to MAX_AUTO_RETRIES times,
            # then require an explicit forge:retry label.
            # Terminal states always require explicit retry regardless of count.
            MAX_AUTO_RETRIES = 3
            retry_count = current_state.get("retry_count", 0)
            cap_reached = retry_count >= MAX_AUTO_RETRIES

            if is_terminal or cap_reached:
                last_error = current_state.get("last_error", "Unknown error")
                reason = (
                    "terminal state" if is_terminal else f"retry cap ({MAX_AUTO_RETRIES}) reached"
                )
                logger.warning(
                    f"Workflow for {message.ticket_key} at '{current_node}' requires "
                    f"forge:retry ({reason})"
                )
                await self._post_terminal_error_comment(message.ticket_key, last_error)
                return current_state
            else:
                # Transient failure — auto-resume and let the node retry
                prev_error = current_state.get("last_error", "")
                logger.info(
                    f"Auto-resuming {message.ticket_key} after error at '{current_node}' "
                    f"(attempt {retry_count + 1}/{MAX_AUTO_RETRIES}): "
                    f"{prev_error[:100] if prev_error else 'unknown'}"
                )
                updated_state["is_paused"] = False
                updated_state["last_error"] = None
        else:
            # Nodes that wait for specific external events should not auto-proceed.
            _signal_required_nodes = (
                "ci_evaluator",
                "attempt_ci_fix",
                "human_review_gate",
                "wait_for_ci_gate",
            )
            if (
                not current_state.get("is_paused", True)
                and current_node not in _signal_required_nodes
            ):
                # Workflow is unpaused at an execution node — let it run.
                # Covers checkpoint patches and nodes that don't need a signal.
                logger.info(
                    f"Workflow for {message.ticket_key} is unpaused at {current_node} "
                    f"— proceeding without explicit signal"
                )
                updated_state["is_paused"] = False
            else:
                # Paused gate with no recognized signal — do not unpause.
                # Covers wrong-stage approvals, unrelated label changes, etc.
                logger.info(
                    f"No valid signal detected for {message.ticket_key} "
                    f"at {current_node} — ignoring event, workflow state unchanged"
                )
                return current_state

        return updated_state

    @staticmethod
    def _extract_text_from_adf(adf: dict) -> str:
        """Extract plain text from Atlassian Document Format."""
        if not isinstance(adf, dict):
            return str(adf) if adf else ""

        texts: list[str] = []

        def _walk(nodes: list[dict]) -> None:
            for node in nodes:
                if node.get("type") == "text":
                    texts.append(node.get("text", ""))
                children = node.get("content")
                if children:
                    _walk(children)

        _walk(adf.get("content", []))
        return " ".join(texts)

    async def _post_skip_gate_feedback(
        self,
        ticket_key: str,
        owner: str,
        repo: str,
        pr_number: int | None,
        check_name: str,
        sender: str,
        action: str,
    ) -> None:
        """Post a GitHub PR reply and Jira audit comment for a skip-gate command.

        Args:
            ticket_key: Jira ticket key for the audit comment.
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            check_name: The check name that was skipped or unskipped.
            sender: GitHub login of the user who issued the command.
            action: "skip" or "unskip".
        """
        try:
            github = GitHubClient()
            jira = JiraClient()
            try:
                if action == "skip":
                    gh_comment = (
                        f"✅ CI gate skipped by @{sender}\n\n"
                        f"The following check will be treated as passing for this PR:\n"
                        f"- `{check_name}`\n\n"
                        f"All other CI checks still apply. "
                        f"Re-evaluating CI status now."
                    )
                    jira_comment = (
                        f"CI gate skipped on GitHub PR by {sender}:\n"
                        f"- `{check_name}`\n\n"
                        f"Skipped via `/forge skip-gate` on PR #{pr_number}. "
                        f"Review accordingly."
                    )
                else:
                    gh_comment = (
                        f"CI gate skip removed by @{sender}\n\n"
                        f"`{check_name}` will be re-evaluated on the next CI run."
                    )
                    jira_comment = (
                        f"CI gate skip removed on GitHub PR by {sender}:\n"
                        f"- `{check_name}`\n\n"
                        f"Check will be re-evaluated on the next CI run."
                    )

                if pr_number:
                    await github.create_issue_comment(owner, repo, pr_number, gh_comment)
                await jira.add_comment(ticket_key, jira_comment)
            finally:
                await github.close()
                await jira.close()
        except Exception as e:
            logger.warning(f"Failed to post skip-gate feedback: {e}")

    async def _post_rebase_feedback(
        self,
        ticket_key: str,
        owner: str,
        repo: str,
        pr_number: int | None,
        sender: str,
    ) -> None:
        """Post feedback for a /forge rebase command."""
        try:
            github = GitHubClient()
            jira = JiraClient()
            try:
                gh_comment = (
                    f"Rebase triggered by @{sender}\n\n"
                    f"Merging `main` into the PR branch and resolving any conflicts. "
                    f"This may take a few minutes."
                )
                jira_comment = (
                    f"Rebase triggered via `/forge rebase` on PR #{pr_number} by {sender}."
                )
                if pr_number:
                    await github.create_issue_comment(owner, repo, pr_number, gh_comment)
                await jira.add_comment(ticket_key, jira_comment)
            finally:
                await github.close()
                await jira.close()
        except Exception as e:
            logger.warning(f"Failed to post rebase feedback: {e}")

    async def _post_terminal_error_comment(self, ticket_key: str, error: str) -> None:
        """Post a comment explaining how to retry a terminal error.

        Args:
            ticket_key: The Jira ticket key.
            error: The error message.
        """
        from forge.integrations.jira.client import JiraClient

        try:
            jira = JiraClient()
            error_preview = error[:200] if error else "Unknown error"
            comment = (
                f"*Forge workflow stopped with error:*\n\n"
                f"{{code}}{error_preview}{{code}}\n\n"
                f"To retry the workflow, add the label `forge:retry` to this ticket."
            )
            await jira.add_comment(ticket_key, comment)
            await jira.close()
            logger.info(f"Posted terminal error comment to {ticket_key}")
        except Exception as e:
            logger.warning(f"Failed to post terminal error comment to {ticket_key}: {e}")

    async def _find_workflow_by_state(self, ticket_key: str) -> tuple[Any, Any]:
        """Find a workflow that has existing checkpoint state for the given ticket.

        Used when the ticket type cannot be determined from the event payload
        (e.g. GitHub webhooks). Checks all registered workflows and returns the
        first one that has a non-empty checkpoint for this ticket.

        Args:
            ticket_key: The Jira ticket key.

        Returns:
            Tuple of (workflow_instance, checkpoint_state), or (None, None) if
            no existing state is found.
        """
        config = {"configurable": {"thread_id": ticket_key}}

        # Read ticket_type from the raw checkpoint bytes — not through a compiled
        # graph's schema, which would apply the schema's default value and lose the
        # stored type (e.g. FeatureState defaults ticket_type to FEATURE).
        # aget() returns the checkpoint dict directly (not an object with .checkpoint).
        # Read ticket_type from the raw bytes — not through a compiled graph's schema,
        # which would apply the schema's default and lose the stored type.
        raw_checkpoint: dict | None = None
        with contextlib.suppress(Exception):
            raw_checkpoint = await self._checkpointer.aget(config)

        if not raw_checkpoint:
            # No checkpoint at all — skip every aget_state call.
            return None, None

        saved_ticket_type: TicketType | None = None
        if isinstance(raw_checkpoint, dict):
            raw_type = raw_checkpoint.get("channel_values", {}).get("ticket_type", "")
            with contextlib.suppress(ValueError):
                saved_ticket_type = TicketType(str(raw_type))

        if saved_ticket_type is not None:
            # Prefer the workflow whose ticket type matches the checkpoint.
            preferred = self.router.resolve(ticket_type=saved_ticket_type, labels=[], event={})
            if preferred is not None:
                compiled = self._get_compiled_workflow(preferred)
                state = await compiled.aget_state(config)
                if state and state.values:
                    logger.debug(
                        f"Found existing state for {ticket_key} in workflow "
                        f"'{preferred.name}' (ticket_type={saved_ticket_type})"
                    )
                    return preferred, state

        # Fallback: return the first workflow with any state.
        for workflow_class in self.router._workflows:
            workflow_instance = workflow_class()
            compiled = self._get_compiled_workflow(workflow_instance)
            state = await compiled.aget_state(config)
            if state and state.values:
                logger.debug(
                    f"Found existing state for {ticket_key} in workflow '{workflow_instance.name}'"
                )
                return workflow_instance, state
        return None, None

    def _extract_ticket_type(self, message: QueueMessage) -> TicketType:
        """Extract ticket type from queue message.

        Args:
            message: The queue message.

        Returns:
            TicketType enum value.
        """
        if message.source == EventSource.JIRA:
            issue_data = message.payload.get("issue", {})
            fields = issue_data.get("fields", {})
            issue_type = fields.get("issuetype", {})
            ticket_type_str = issue_type.get("name", "Unknown")

            # Child ticket events (Epic, Task) are re-routed to the parent Feature
            # by the Jira webhook handler. The payload still carries the child's
            # issue type, which won't match any workflow. Fall through to UNKNOWN
            # so _find_workflow_by_state resolves it from checkpoint.
            child_types = {"Epic", "Task", "Sub-task"}
            if ticket_type_str in child_types:
                return TicketType.UNKNOWN

            # Map string to TicketType enum
            try:
                return TicketType(ticket_type_str)
            except ValueError:
                logger.warning(f"Unknown ticket type '{ticket_type_str}' for {message.ticket_key}")
                return TicketType.UNKNOWN

        return TicketType.UNKNOWN

    def _get_compiled_workflow(self, workflow_instance: Any) -> Any:
        """Get or compile a workflow graph.

        Args:
            workflow_instance: A BaseWorkflow instance.

        Returns:
            Compiled workflow graph.
        """
        workflow_name = workflow_instance.name

        # Check cache
        if workflow_name in self._compiled_workflows:
            return self._compiled_workflows[workflow_name]

        # Build and compile the workflow graph
        logger.info(f"Compiling workflow: {workflow_name}")
        graph = workflow_instance.build_graph()
        compiled = graph.compile(checkpointer=self._checkpointer)

        # Cache it
        self._compiled_workflows[workflow_name] = compiled

        return compiled

    def _build_initial_state(self, message: QueueMessage) -> dict[str, Any]:
        """Build initial workflow state from queue message.

        Args:
            message: The queue message.

        Returns:
            Initial state dictionary.
        """
        # Extract ticket type from payload
        ticket_type = "Unknown"  # Require explicit type, don't default to Feature
        if message.source == EventSource.JIRA:
            issue_data = message.payload.get("issue", {})
            fields = issue_data.get("fields", {})
            issue_type = fields.get("issuetype", {})
            ticket_type = issue_type.get("name", "Unknown")

        # Validate ticket type - only Features and Bugs can start workflows directly
        valid_top_level_types = ("Feature", "Bug", "Story")
        if ticket_type not in valid_top_level_types:
            logger.warning(
                f"Ticket {message.ticket_key} has type '{ticket_type}' which cannot "
                f"start a workflow directly. Valid types: {valid_top_level_types}"
            )

        return {
            "ticket_key": message.ticket_key,
            "ticket_type": ticket_type,
            "event_type": message.event_type,
            "context": {
                "source": message.source.value,
                "event_id": message.event_id,
                "payload": message.payload,
            },
            "current_node": "entry",
            "is_paused": False,
            "retry_count": message.retry_count,
        }

    async def start(self) -> None:
        """Start the worker and begin processing events."""
        logger.info(f"Starting orchestrator worker: {self.consumer_name}")

        # Start Prometheus metrics HTTP server
        if self.settings.worker_metrics_enabled:
            from prometheus_client import start_http_server

            metrics_port = self.settings.worker_metrics_port
            start_http_server(metrics_port)
            logger.info(f"Worker metrics server started on port {metrics_port}")

        # Initialize checkpointer
        self._checkpointer = await get_checkpointer()
        logger.info("Checkpointer initialized")

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Register handlers
        self.consumer.register_handler(EventSource.JIRA, self._handle_jira_event)
        self.consumer.register_handler(EventSource.GITHUB, self._handle_github_event)

        try:
            await self.consumer.start()
        except asyncio.CancelledError:
            pass
        finally:
            await self.consumer.stop()
            logger.info("Worker shut down gracefully")

    def _handle_shutdown(self) -> None:
        """Handle shutdown signal."""
        logger.info("Shutdown signal received")
        asyncio.create_task(self.consumer.stop())


async def run_single_ticket(ticket_key: str) -> dict[str, Any]:
    """Run workflow for a single ticket (for testing/CLI use).

    Args:
        ticket_key: The Jira ticket key to process.

    Returns:
        Final workflow state.
    """
    from forge.integrations.jira.client import JiraClient

    logger.info(f"Running workflow for {ticket_key}")

    # Fetch ticket to determine type
    jira = JiraClient()
    try:
        issue = await jira.get_issue(ticket_key)
        ticket_type_str = issue.issue_type
        # Convert string to TicketType enum
        try:
            ticket_type = TicketType(ticket_type_str)
        except ValueError:
            logger.warning(f"Unknown ticket type '{ticket_type_str}', using UNKNOWN")
            ticket_type = TicketType.UNKNOWN
    finally:
        await jira.close()

    # Create router and resolve workflow
    router = create_default_router()
    workflow_instance = router.resolve(
        ticket_type=ticket_type,
        labels=[],
        event={},
    )

    if workflow_instance is None:
        raise ValueError(f"No workflow found for ticket type: {ticket_type}")

    # Build and compile workflow
    checkpointer = await get_checkpointer()
    graph = workflow_instance.build_graph()
    compiled_workflow = graph.compile(checkpointer=checkpointer)

    initial_state = {
        "ticket_key": ticket_key,
        "ticket_type": ticket_type_str,
        "event_type": "manual_trigger",
        "context": {},
        "current_node": "entry",
        "is_paused": False,
        "retry_count": 0,
    }

    # Use ticket_key as thread_id for checkpointing
    config = {"configurable": {"thread_id": ticket_key}}

    result = await compiled_workflow.ainvoke(initial_state, config=config)
    logger.info(f"Workflow completed: {result.get('current_node')}")
    return result


def main() -> None:
    """Main entry point for the worker."""
    from dotenv import load_dotenv

    load_dotenv()  # must happen before basicConfig reads LOG_LEVEL
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Check for single-ticket mode via command line
    if len(sys.argv) > 1:
        ticket_key = sys.argv[1]
        asyncio.run(run_single_ticket(ticket_key))
    else:
        # Run as continuous worker
        worker = OrchestratorWorker()
        asyncio.run(worker.start())


if __name__ == "__main__":
    main()
