"""Orchestrator worker that consumes events from Redis and processes them."""

import asyncio
import contextlib
import logging
import os
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
from forge.domain import (
    EffectCommand,
    JsonValue,
    ResourceIdentity,
    WorkflowIdentity,
    stable_identity,
)
from forge.effects import EffectService, create_default_effect_service
from forge.effects.jira import (
    JIRA_COMMENT_OPERATION,
)
from forge.effects.source_control import SC_COMMENT_CREATE_OPERATION
from forge.integrations.jira.client import JiraClient
from forge.integrations.source_control.contracts import RepositoryRef
from forge.integrations.source_control.registry import get_registry
from forge.models.events import EventSource
from forge.models.workflow import ForgeLabel, TicketType
from forge.orchestrator.checkpointer import get_checkpointer, get_ticket_from_pr_index
from forge.orchestrator.command_handlers import (
    CommandHandlerRegistry,
    create_default_command_handler_registry,
)
from forge.orchestrator.event_adapters import (
    AdaptedEvent,
    CommandDecision,
    EventAdapterRegistry,
    create_default_event_adapter_registry,
    interpret_event,
    record_command_decision,
    validate_command_decision,
)
from forge.orchestrator.review_enrichment import ReviewEnrichmentService
from forge.queue.consumer import QueueConsumer
from forge.queue.models import QueueMessage
from forge.reconciliation import (
    ObservationDisposition,
    ObservationLedger,
    RedisObservationLedger,
)
from forge.skills.orchestrator import ensure_skills
from forge.skills.utils import extract_project_key
from forge.utils.redaction import redact_secrets
from forge.workflow.command_operations import execute_command_operation
from forge.workflow.declarative.compiler import WorkflowValidationError
from forge.workflow.declarative.resolver import (
    load_project_workflow,
    selected_workflow_name,
)
from forge.workflow.declarative.workflow import DeclarativeWorkflow
from forge.workflow.effect_runtime import bind_effect_runtime
from forge.workflow.nodes.error_handler import notify_error
from forge.workflow.nodes.workspace_setup import teardown_workspace
from forge.workflow.pr_state import save_active_pull_request
from forge.workflow.registry import create_default_router
from forge.workflow.router import WorkflowRouter
from forge.workflow.transitions import (
    ObservationTransitionPolicy,
    apply_observation_transition,
)
from forge.workflow.utils.jira_status import post_status_comment  # noqa: F401
from forge.workflow.utils.source_control import get_adapter

logger = logging.getLogger(__name__)

_CI_STAGES = ("ci_evaluator", "attempt_ci_fix", "human_review_gate")


def _is_workflow_errored(state: dict) -> bool:
    """Return True when workflow has a recorded error and is not paused for human input."""
    return not state.get("is_paused") and state.get("last_error") is not None


def _has_new_reportable_error(result: dict, error_before_invoke: str | None) -> bool:
    """Return whether an invocation produced an error that should be reported."""
    last_error = result.get("last_error")
    return bool(last_error and last_error != error_before_invoke)


async def _report_new_workflow_error(result: dict, error_before_invoke: str | None) -> None:
    """Post one notification when an invocation produces a reportable error."""
    if not _has_new_reportable_error(result, error_before_invoke):
        return

    await notify_error(
        result,
        result["last_error"],
        result.get("current_node", "unknown"),
    )


async def _cleanup_terminal_workspace(result: dict[str, Any]) -> dict[str, Any]:
    """Remove a workspace recreated after the normal post-PR teardown."""
    if result.get("current_node") != "complete" or not result.get("workspace_path"):
        return result

    cleaned = await teardown_workspace(result)
    return {
        **cleaned,
        "current_node": "complete",
        "is_paused": False,
    }


_FRESH_INVOKE_NODES = (
    "ci_evaluator",
    "attempt_ci_fix",
    "human_review_gate",
    "setup_workspace",
)


# Matches >option N anywhere in comment (case-insensitive, first match wins)
# Supports both start-of-line usage (>option 2) and in-prose usage (let's go with >option 2)
class OrchestratorWorker:
    """Worker that processes workflow events from Redis queue."""

    def __init__(
        self,
        consumer_name: str | None = None,
        router: WorkflowRouter | None = None,
        event_adapters: EventAdapterRegistry | None = None,
        command_handlers: CommandHandlerRegistry | None = None,
        review_enrichment: ReviewEnrichmentService | None = None,
        effect_service: EffectService | None = None,
        observation_ledger: ObservationLedger | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            consumer_name: Unique name for this consumer. Auto-generated if not provided.
            router: WorkflowRouter for selecting workflows. Uses default if not provided.
        """
        self.settings = get_settings()
        self.consumer_name = consumer_name or f"worker-{uuid.uuid4().hex[:8]}"
        self.consumer = QueueConsumer(
            self.consumer_name,
            terminal_failure_handler=self._handle_terminal_failure,
        )
        self.router = router or create_default_router()
        self.event_adapters = event_adapters or create_default_event_adapter_registry()
        self.command_handlers = command_handlers or create_default_command_handler_registry()
        self.review_enrichment = review_enrichment
        self.effect_service = effect_service or create_default_effect_service()
        self.observation_ledger = observation_ledger or RedisObservationLedger()
        self._shutdown_event = asyncio.Event()
        self._checkpointer = None
        self._compiled_workflows: dict[str, Any] = {}  # Cache compiled workflows by name
        # Keyed by connection name -- a different connection may authenticate
        # as a different bot identity, so a single process-wide login is wrong
        # once more than the default connection is configured.
        self._forge_github_logins: dict[str, str] = {}

    def _review_enrichment(self) -> ReviewEnrichmentService:
        service = getattr(self, "review_enrichment", None)
        if service is None:
            service = ReviewEnrichmentService(get_adapter)
            self.review_enrichment = service
        return service

    def _transition_settings(self) -> Any:
        """Provide configuration to the observation transition runtime."""
        return get_settings()

    def _event_adapter_registry(self) -> EventAdapterRegistry:
        """Lazily restore adapters for legacy fixtures that bypass ``__init__``."""
        registry = getattr(self, "event_adapters", None)
        if registry is None:
            registry = create_default_event_adapter_registry()
            self.event_adapters = registry
        return registry

    def _durable_effect_service(self) -> EffectService:
        """Lazily restore the effect runtime for legacy fixtures."""
        service = getattr(self, "effect_service", None)
        if service is None:
            service = create_default_effect_service()
            self.effect_service = service
        return service

    async def _invoke_workflow(
        self,
        compiled_workflow: Any,
        invocation_input: dict[str, Any] | None,
        *,
        config: dict[str, Any],
        ticket_key: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke graph code with the durable effect port bound to this run."""
        identity = WorkflowIdentity(
            run_id=str(state.get("thread_id") or ticket_key),
            workflow_name=str(state.get("workflow_name") or state.get("ticket_type") or "legacy"),
            definition_revision=int(
                state.get("workflow_definition_revision") or state.get("workflow_revision") or 1
            ),
            definition_digest=state.get("workflow_definition_digest"),
        )
        with bind_effect_runtime(self._durable_effect_service(), identity):
            return await compiled_workflow.ainvoke(invocation_input, config=config)

    def _observation_ledger(self) -> ObservationLedger:
        """Lazily restore reconciliation for fixtures that bypass ``__init__``."""
        ledger = getattr(self, "observation_ledger", None)
        if ledger is None:
            ledger = RedisObservationLedger()
            self.observation_ledger = ledger
        return ledger

    async def _execute_required_jira_effect(
        self,
        *,
        ticket_key: str,
        state: dict[str, Any],
        event_id: str,
        operation: str,
        payload: dict[str, JsonValue],
        logical_action: str,
    ) -> None:
        identity_parts: dict[str, JsonValue] = {
            "run_id": ticket_key,
            "event_id": event_id,
            "operation": operation,
            "logical_action": logical_action,
            "target": ticket_key,
        }
        effect_id = stable_identity("effect", identity_parts)
        await self._durable_effect_service().execute_required(
            EffectCommand(
                effect_id=effect_id,
                idempotency_key=effect_id,
                workflow=WorkflowIdentity(
                    run_id=str(state.get("thread_id") or ticket_key),
                    workflow_name=str(
                        state.get("workflow_name") or state.get("ticket_type") or "legacy"
                    ),
                    definition_revision=int(
                        state.get("workflow_definition_revision")
                        or state.get("workflow_revision")
                        or 1
                    ),
                    definition_digest=state.get("workflow_definition_digest"),
                ),
                operation=operation,
                target=ResourceIdentity(resource_type="issue", external_id=ticket_key),
                payload=payload,
            )
        )

    async def _execute_required_comment(
        self,
        ticket_key: str,
        body: str,
        *,
        logical_action: str,
        discriminator: str = "",
    ) -> None:
        await self._execute_required_jira_effect(
            ticket_key=ticket_key,
            state={},
            event_id=discriminator,
            operation=JIRA_COMMENT_OPERATION,
            payload={"body": body},
            logical_action=logical_action,
        )

    async def _execute_required_source_comment(
        self,
        repo_ref: RepositoryRef,
        pr_number: int,
        body: str,
        *,
        ticket_key: str,
        logical_action: str,
    ) -> None:
        identity = {
            "run_id": ticket_key,
            "operation": SC_COMMENT_CREATE_OPERATION,
            "repository": repo_ref.namespace,
            "pull_request": pr_number,
            "logical_action": logical_action,
        }
        effect_id = stable_identity("effect", identity)
        await self._durable_effect_service().execute_required(
            EffectCommand(
                effect_id=effect_id,
                idempotency_key=effect_id,
                workflow=WorkflowIdentity(
                    run_id=ticket_key,
                    workflow_name="legacy",
                    definition_revision=1,
                ),
                operation=SC_COMMENT_CREATE_OPERATION,
                target=ResourceIdentity(
                    resource_type="change_request",
                    external_id=str(pr_number),
                    namespace=repo_ref.namespace,
                ),
                payload={"body": body},
            )
        )

    async def _get_forge_github_login(self, repo_ref: RepositoryRef) -> str:
        """Resolve and cache the authenticated Forge identity for this connection."""
        cached = self._forge_github_logins.get(repo_ref.connection)
        if cached:
            return cached
        _, adapter = get_adapter(repo_ref.namespace)
        identity = await adapter.get_authenticated_identity(repo_ref)
        if identity.login:
            self._forge_github_logins[repo_ref.connection] = identity.login
        return identity.login

    async def _handle_terminal_failure(self, message: QueueMessage, error: str) -> None:
        """Post one Jira comment after queue retries are exhausted."""
        event_marker = f"Event/correlation ID: {message.event_id}"
        safe_error = redact_secrets(error)
        if len(safe_error) > 500:
            safe_error = f"{safe_error[:500]}..."
        details = (
            "**Forge error in queue execution (retries exhausted):**\n\n"
            f"{safe_error}\n\n"
            f"Ticket: {message.ticket_key}\n"
            f"{event_marker}\n"
            "Recovery: inspect the dead-letter entry, resolve the root cause, "
            "then requeue the event."
        )
        await self._execute_required_comment(
            message.ticket_key,
            details,
            logical_action="terminal-queue-failure",
            discriminator=message.event_id,
        )
        logger.info(f"Posted terminal queue failure notification to {message.ticket_key}")

    async def _handle_event(self, message: QueueMessage) -> None:
        """Handle any registered ingress source through its adapter."""
        adapted = self._event_adapter_registry().adapt(message)
        if adapted.requires_ticket_correlation:
            message = await self._resolve_ticket_from_pr_index(message)
            if not message.ticket_key:
                logger.info(
                    f"Dropping source-control event {message.event_id}: "
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
        adapted = self._event_adapter_registry().adapt(message)
        pr_url = adapted.change_request_url

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
            await ensure_skills(
                project_key,
                jira_client,
                skills_dir,
                skills_install_dir=self.settings.skills_install_dir,
            )
        except Exception:
            logger.warning(
                "Skill synchronisation failed for %s; continuing with workflow.",
                ticket_key,
                exc_info=True,
            )

        try:
            ingress = self.event_adapters.adapt(message)
            observation_decision = await self._observation_ledger().record(ingress.observation)
            if observation_decision.disposition in {
                ObservationDisposition.DUPLICATE,
                ObservationDisposition.STALE,
                ObservationDisposition.CONFLICT,
            }:
                logger.info(
                    "Ignoring %s observation %s: %s",
                    observation_decision.disposition.value,
                    ingress.observation.observation_id,
                    observation_decision.reason,
                )
                return
            # Determine ticket type early to select workflow
            ticket_type = self._extract_ticket_type(message)

            workflow_instance: Any = None
            existing_state = None
            config: dict[str, Any] = {"configurable": {"thread_id": ticket_key}}

            observed_issue = ingress.observation.facts.get("issue", {})
            labels = (
                observed_issue.get("fields", {}).get("labels", [])
                if isinstance(observed_issue, dict)
                else []
            ) or []
            try:
                custom_workflow = await self._resolve_custom_workflow(ticket_key, labels)
            except Exception as exc:
                await self._report_custom_workflow_configuration_error(ticket_key, str(exc))
                return

            if custom_workflow is not None:
                if not custom_workflow.supports_ticket_type(ticket_type):
                    await self._report_custom_workflow_configuration_error(
                        ticket_key,
                        f"workflow '{custom_workflow.name}' uses state profile "
                        f"'{custom_workflow.definition.spec.state}', which is incompatible with "
                        f"ticket type '{ticket_type.value}'",
                    )
                    return
                workflow_instance = custom_workflow
                config["recursion_limit"] = 100
            elif ticket_type == TicketType.UNKNOWN:
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
                    labels=labels,
                    event=dict(ingress.observation.facts),
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

            if (
                isinstance(workflow_instance, DeclarativeWorkflow)
                and existing_state
                and existing_state.values
            ):
                values = dict(existing_state.values)
                # A pinned artifact is immutable: validate it and continue on
                # that exact graph. Revision adoption belongs to the explicit
                # migration operation, never to ordinary event handling.
                try:
                    status = workflow_instance.pin_status(values)
                    if status == "pinned":
                        workflow_instance.validate_pinned_state(values)
                    elif status == "legacy_unpinned":
                        raise WorkflowValidationError(
                            "checkpoint requires the explicit Phase 8 definition-pinning migration"
                        )
                except Exception as exc:
                    await self._report_custom_workflow_configuration_error(ticket_key, str(exc))
                    return

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
                adapted_event = self.event_adapters.adapt(message)
                command_decision = interpret_event(message, adapted_event, existing_state.values)
                command_decision = validate_command_decision(
                    command_decision, existing_state.values
                )
                updated_values = await self._apply_observation_transition(
                    message,
                    existing_state.values,
                    adapted_event=adapted_event,
                    command_decision=command_decision,
                    policy=(
                        ObservationTransitionPolicy(
                            identifier=workflow_instance.resolve_observation_policy() or "default",
                            definition=workflow_instance.definition.canonical_dict(),
                        )
                        if isinstance(workflow_instance, DeclarativeWorkflow)
                        else ObservationTransitionPolicy()
                    ),
                )
                if (
                    updated_values is not existing_state.values
                    and command_decision.command is not None
                    and command_decision.command.command_type.value == "rebase"
                ):
                    updated_values = await execute_command_operation(
                        command_decision.command, updated_values
                    )
                state_changed = updated_values is not existing_state.values
                updated_values = record_command_decision(
                    updated_values,
                    message=message,
                    adapted=adapted_event,
                    decision=command_decision,
                )

                # _apply_observation_transition returns early (unchanged current_node) when
                # the workflow is at a terminal state without an explicit retry signal.
                # In that case just persist the state update and stop.
                # and stop — don't try to invoke a finished graph.
                terminal_nodes = ("complete",)
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

                # If _apply_observation_transition returned the state object unchanged (identity
                # check), no signal was recognised — do not invoke the workflow.
                # Without this guard, nodes in needs_fresh_invoke (e.g. human_review_gate)
                # would be re-invoked with is_paused=True and immediately re-pause,
                # producing a misleading "Resuming workflow" log with no real effect.
                if not state_changed:
                    await compiled_workflow.aupdate_state(config, updated_values)
                    return

                logger.info(f"Resuming workflow for {ticket_key}")

                was_errored = _is_workflow_errored(existing_state.values)
                resume_context = updated_values.get("context", {})
                force_fresh_invoke = bool(resume_context.get("force_fresh_invoke"))
                if force_fresh_invoke:
                    updated_values = {
                        **updated_values,
                        "context": {
                            **resume_context,
                        },
                    }
                    updated_values["context"].pop("force_fresh_invoke", None)

                # Nodes that wait for external events or need their body re-run
                # must be re-invoked fresh so route_by_ticket_type re-runs them.
                # ainvoke(None) only replays the routing edge after the node, not
                # the node itself, so setup/retry work would never be attempted.
                needs_fresh_invoke = (
                    force_fresh_invoke or updated_values.get("current_node") in _FRESH_INVOKE_NODES
                )

                error_before_invoke = updated_values.get("last_error")

                if was_errored or needs_fresh_invoke:
                    logger.info(
                        f"{'Retrying' if was_errored else 'Re-invoking'} workflow "
                        f"from {updated_values.get('current_node')}"
                    )
                    result = await self._invoke_workflow(
                        compiled_workflow,
                        updated_values,
                        config=config,
                        ticket_key=ticket_key,
                        state=updated_values,
                    )
                else:
                    # For normal resume (paused at approval gate): update state and continue
                    await compiled_workflow.aupdate_state(config, updated_values)
                    result = await self._invoke_workflow(
                        compiled_workflow,
                        None,
                        config=config,
                        ticket_key=ticket_key,
                        state=updated_values,
                    )
            else:
                error_before_invoke = None

                # New workflow - build initial state
                state = self._build_initial_state(message, workflow_instance)
                logger.info(f"Starting new workflow for {ticket_key}")

                # Record workflow started metric
                ticket_type_str = state.get("ticket_type", "unknown")
                record_workflow_started(ticket_type=ticket_type_str)

                # Run the workflow from the beginning
                result = await self._invoke_workflow(
                    compiled_workflow,
                    state,
                    config=config,
                    ticket_key=ticket_key,
                    state=state,
                )

            cleaned_result = await _cleanup_terminal_workspace(result)
            if cleaned_result != result:
                await compiled_workflow.aupdate_state(config, cleaned_result)
                result = cleaned_result

            # Nodes continue to use scalar PR fields as a compatibility view.
            # Persist that view back into the selected per-repository record
            # after every invocation so subsequent webhooks restore fresh CI,
            # review, and merge state for the PR they target.
            persisted_result = save_active_pull_request(result)
            if persisted_result != result:
                await compiled_workflow.aupdate_state(config, persisted_result)
                result = persisted_result

            final_node = result.get("current_node", "unknown")
            is_paused = result.get("is_paused", False)
            logger.info(
                f"Workflow completed for {ticket_key}, "
                f"final node: {final_node}, "
                f"paused: {is_paused}"
            )

            # Report errors to Jira — only if the error is new (not carried
            # over from a previous invocation that already reported it).
            await _report_new_workflow_error(result, error_before_invoke)

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

    async def _apply_observation_transition(
        self,
        message: QueueMessage,
        current_state: dict[str, Any],
        *,
        adapted_event: AdaptedEvent | None = None,
        command_decision: CommandDecision | None = None,
        policy: ObservationTransitionPolicy | None = None,
    ) -> dict[str, Any]:
        """Handle a resume event for a paused workflow.

        Detects approval/rejection signals from the webhook payload.

        Args:
            message: The queue message.
            current_state: Current workflow state from checkpoint.

        Returns:
            Updated state for workflow resumption.
        """
        return await apply_observation_transition(
            self,
            message,
            current_state,
            adapted_event=adapted_event,
            command_decision=command_decision,
            policy=policy or ObservationTransitionPolicy(),
        )

    async def _post_resume_ack_comment(
        self,
        ticket_key: str,
        signal_type: str,
        current_node: str,
        source_ticket_key: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """Post a best-effort Jira acknowledgement for user-visible resume signals."""
        stage = self._stage_label_for_node(current_node)
        source_suffix = (
            f" from {source_ticket_key}"
            if source_ticket_key and source_ticket_key != ticket_key
            else ""
        )
        comment_target_key = (
            source_ticket_key
            if source_ticket_key and source_ticket_key != ticket_key
            else ticket_key
        )

        if signal_type == "question":
            message = (
                f"❓ Forge received your question about {stage}{source_suffix} "
                "and is preparing an answer."
            )
        else:
            message = (
                f"♻️ Forge received your revision request for {stage}{source_suffix} "
                "and is regenerating the artifact."
            )

        identity_parts: dict[str, JsonValue] = {
            "ticket_key": ticket_key,
            "target": comment_target_key,
            "signal_type": signal_type,
            "current_node": current_node,
            "event_id": event_id or "legacy",
        }
        effect_id = stable_identity("effect", identity_parts)
        command = EffectCommand(
            effect_id=effect_id,
            idempotency_key=effect_id,
            workflow=WorkflowIdentity(
                run_id=ticket_key,
                workflow_name="legacy",
                definition_revision=1,
            ),
            operation="jira.comment.create",
            target=ResourceIdentity(resource_type="issue", external_id=comment_target_key),
            payload={"body": message},
        )
        await self._durable_effect_service().submit(command)

    @staticmethod
    def _stage_label_for_node(current_node: str) -> str:
        """Return a human-readable workflow stage for an approval/review node."""
        node_to_stage = {
            "prd_approval_gate": "the PRD",
            "generate_prd": "the PRD",
            "regenerate_prd": "the PRD",
            "spec_approval_gate": "the spec",
            "generate_spec": "the spec",
            "regenerate_spec": "the spec",
            "plan_approval_gate": "the plan",
            "decompose_epics": "the plan",
            "regenerate_all_epics": "the plan",
            "update_single_epic": "the plan",
            "rca_option_gate": "the RCA",
            "plan_approval_gate_bug": "the plan",
            "task_plan_approval_gate": "the task plan",
            "task_approval_gate": "the tasks",
            "generate_tasks": "the tasks",
            "regenerate_all_tasks": "the tasks",
            "regenerate_epic_tasks": "the tasks",
            "update_single_task": "the task",
            "human_review_gate": "the implementation review",
            "review_response_gate": "the implementation review",
        }
        return node_to_stage.get(current_node, "the current workflow stage")

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
        repo_ref: RepositoryRef,
        pr_number: int | None,
        check_name: str,
        sender: str,
        action: str,
    ) -> None:
        """Post a GitHub PR reply and Jira audit comment for a skip-gate command.

        Args:
            ticket_key: Jira ticket key for the audit comment.
            repo_ref: Repository reference the PR belongs to.
            pr_number: Pull request number.
            check_name: The check name that was skipped or unskipped.
            sender: GitHub login of the user who issued the command.
            action: "skip" or "unskip".
        """
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
                await self._execute_required_source_comment(
                    repo_ref,
                    pr_number,
                    gh_comment,
                    ticket_key=ticket_key,
                    logical_action=f"ci-gate-{action}:{check_name}",
                )
            await self._execute_required_comment(
                ticket_key,
                jira_comment,
                logical_action=f"ci-gate-{action}:{repo_ref.namespace}:{pr_number}:{check_name}",
            )
        except Exception as e:
            logger.warning(f"Failed to post skip-gate feedback: {e}")

    async def _post_rebase_feedback(
        self,
        ticket_key: str,
        repo_ref: RepositoryRef,
        pr_number: int | None,
        sender: str,
    ) -> None:
        """Post feedback for a /forge rebase command."""
        try:
            gh_comment = (
                f"Rebase triggered by @{sender}\n\n"
                f"Merging `main` into the PR branch and resolving any conflicts. "
                f"This may take a few minutes."
            )
            jira_comment = f"Rebase triggered via `/forge rebase` on PR #{pr_number} by {sender}."
            if pr_number:
                await self._execute_required_source_comment(
                    repo_ref,
                    pr_number,
                    gh_comment,
                    ticket_key=ticket_key,
                    logical_action="rebase-acknowledgement",
                )
            await self._execute_required_comment(
                ticket_key,
                jira_comment,
                logical_action=f"rebase-acknowledgement:{repo_ref.namespace}:{pr_number}",
            )
        except Exception as e:
            logger.warning(f"Failed to post rebase feedback: {e}")

    async def _post_terminal_error_comment(self, ticket_key: str, error: str) -> None:
        """Post a comment explaining how to retry a terminal error.

        Args:
            ticket_key: The Jira ticket key.
            error: The error message.
        """
        try:
            safe_error = redact_secrets(error) if error else "Unknown error"
            error_preview = safe_error[:200]
            comment = (
                f"**Forge workflow stopped with error:**\n\n"
                f"```\n{error_preview}\n```\n\n"
                f"To retry the workflow, add the label `forge:retry` to this ticket."
            )
            await self._execute_required_comment(
                ticket_key,
                comment,
                logical_action=f"terminal-workflow-error:{error_preview}",
            )
            logger.info(f"Posted terminal error comment to {ticket_key}")
        except Exception as e:
            logger.warning(f"Failed to post terminal error comment to {ticket_key}: {e}")

    async def _post_retry_acknowledgement(self, ticket_key: str, node: str) -> None:
        """Acknowledge an accepted retry without blocking workflow resumption."""
        try:
            comment = (
                f"Forge accepted the `forge:retry` request and is resuming "
                f"the workflow from `{node}`."
            )
            await self._execute_required_comment(
                ticket_key,
                comment,
                logical_action=f"retry-acknowledgement:{node}",
            )
            logger.info(f"Posted retry acknowledgement to {ticket_key}")
        except Exception as e:
            logger.warning(f"Failed to post retry acknowledgement to {ticket_key}: {e}")

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

    async def _resolve_custom_workflow(
        self, ticket_key: str, labels: list[str]
    ) -> DeclarativeWorkflow | None:
        """Resolve a pinned identity or a workflow selected by a label.

        Pinned checkpoints carry their canonical artifact, so resuming one does
        not consult the mutable Jira project property. Identity-only checkpoints
        use the publication store and fail closed if that exact artifact is
        unavailable.
        """
        raw_checkpoint: dict[str, Any] | None = None
        config = {"configurable": {"thread_id": ticket_key}}
        with contextlib.suppress(Exception):
            raw_checkpoint = await self._checkpointer.aget(config)
        values = raw_checkpoint.get("channel_values", {}) if raw_checkpoint else {}

        workflow_name = values.get("workflow_name")
        project_key = values.get("workflow_project_key")
        if workflow_name:
            project_key = project_key or ticket_key.split("-", 1)[0]
        else:
            workflow_name = selected_workflow_name(labels)
            project_key = ticket_key.split("-", 1)[0] if workflow_name else None
        if not workflow_name:
            return None

        revision = values.get("workflow_definition_revision", values.get("workflow_revision"))
        digest = values.get("workflow_definition_digest", values.get("workflow_digest"))
        canonical = values.get("workflow_definition")
        if revision is not None or digest is not None or canonical is not None:
            from forge.workflow.declarative.publication import DefinitionPublisher

            return await load_project_workflow(
                None,
                str(project_key),
                str(workflow_name),
                pinned_revision=int(revision) if revision is not None else None,
                pinned_digest=str(digest) if digest is not None else None,
                pinned_definition=canonical,
                definition_reader=DefinitionPublisher(str(project_key)),
            )

        jira = JiraClient()
        try:
            from forge.workflow.declarative.publication import DefinitionPublisher

            return await load_project_workflow(
                jira,
                str(project_key),
                str(workflow_name),
                definition_reader=DefinitionPublisher(str(project_key)),
            )
        finally:
            await jira.close()

    async def _report_custom_workflow_configuration_error(
        self, ticket_key: str, error: str
    ) -> None:
        """Fail closed with an actionable, redacted Jira comment."""
        try:
            await self._execute_required_comment(
                ticket_key,
                f"**Forge custom workflow configuration error:**\n\n{redact_secrets(error)[:1000]}",
                logical_action=f"custom-workflow-configuration:{error}",
            )
        except Exception:
            logger.warning(
                "Could not report custom workflow error for %s", ticket_key, exc_info=True
            )

    def _extract_ticket_type(self, message: QueueMessage) -> TicketType:
        """Extract ticket type from queue message.

        Args:
            message: The queue message.

        Returns:
            TicketType enum value.
        """
        if message.source != EventSource.JIRA:
            return TicketType.UNKNOWN
        return self._event_adapter_registry().adapt(message).ticket_type

    def _get_compiled_workflow(self, workflow_instance: Any) -> Any:
        """Get or compile a workflow graph.

        Args:
            workflow_instance: A BaseWorkflow instance.

        Returns:
            Compiled workflow graph.
        """
        workflow_name = getattr(workflow_instance, "cache_key", workflow_instance.name)

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

    def _build_initial_state(
        self, message: QueueMessage, workflow_instance: Any | None = None
    ) -> dict[str, Any]:
        """Build initial workflow state from queue message.

        Args:
            message: The queue message.

        Returns:
            Initial state dictionary.
        """
        # Extract ticket type and labels from normalized observation evidence.
        ticket_type = "Unknown"  # Require explicit type, don't default to Feature
        labels: list[str] = []
        observation_id = f"transport:{message.event_id}"
        if message.source == EventSource.JIRA:
            adapters = (
                getattr(self, "event_adapters", None) or create_default_event_adapter_registry()
            )
            adapted = adapters.adapt(message)
            observation_id = adapted.observation.observation_id
            issue_data = adapted.observation.facts.get("issue", {})
            fields = issue_data.get("fields", {})
            issue_type = fields.get("issuetype", {})
            ticket_type = issue_type.get("name", "Unknown")
            labels = fields.get("labels", [])

        # Validate ticket type - only Features and Bugs can start workflows directly
        valid_top_level_types = ("Feature", "Bug", "Story")
        if ticket_type not in valid_top_level_types:
            logger.warning(
                f"Ticket {message.ticket_key} has type '{ticket_type}' which cannot "
                f"start a workflow directly. Valid types: {valid_top_level_types}"
            )

        yolo_mode = ForgeLabel.YOLO in labels

        event_state = {
            "ticket_key": message.ticket_key,
            "ticket_type": ticket_type,
            "event_type": message.event_type,
            "context": {
                "source": message.source.value,
                "event_id": message.event_id,
                "observation_id": observation_id,
            },
            "current_node": "entry",
            "is_paused": False,
            "retry_count": message.retry_count,
            "yolo_mode": yolo_mode,
        }
        if isinstance(workflow_instance, DeclarativeWorkflow):
            initial = workflow_instance.create_initial_state(message.ticket_key)
            return {**initial, **event_state}
        return event_state

    async def start(self) -> None:
        """Start the worker and begin processing events."""
        from forge.utils.logging import log_startup_banner

        log_startup_banner("Queue Worker")

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

        # Every registered source follows the same transport path. Adding an
        # adapter does not require another worker branch.
        for source in self._event_adapter_registry().sources:
            self.consumer.register_handler(source, self._handle_event)

        effect_stop = asyncio.Event()
        effect_task = asyncio.create_task(self._durable_effect_service().run_forever(effect_stop))
        try:
            await self.consumer.start()
        except asyncio.CancelledError:
            pass
        finally:
            effect_stop.set()
            await effect_task
            await self.consumer.stop()
            await get_registry().aclose()
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
    checkpointer = await get_checkpointer()
    checkpoint_config = {"configurable": {"thread_id": ticket_key}}
    raw_checkpoint = await checkpointer.aget(checkpoint_config)
    checkpoint_values = raw_checkpoint.get("channel_values", {}) if raw_checkpoint else {}

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
        workflow_name = checkpoint_values.get("workflow_name") or selected_workflow_name(
            issue.labels
        )
        if workflow_name:
            from forge.workflow.declarative.publication import DefinitionPublisher

            workflow_instance: Any
            project_key = (
                checkpoint_values.get("workflow_project_key")
                or issue.project_key
                or ticket_key.split("-", 1)[0]
            )
            revision = checkpoint_values.get(
                "workflow_definition_revision", checkpoint_values.get("workflow_revision")
            )
            digest = checkpoint_values.get(
                "workflow_definition_digest", checkpoint_values.get("workflow_digest")
            )
            canonical = checkpoint_values.get("workflow_definition")
            if revision is not None or digest is not None or canonical is not None:
                workflow_instance = await load_project_workflow(
                    None,
                    project_key,
                    workflow_name,
                    pinned_revision=int(revision) if revision is not None else None,
                    pinned_digest=str(digest) if digest is not None else None,
                    pinned_definition=canonical,
                    definition_reader=DefinitionPublisher(project_key),
                )
            else:
                workflow_instance = await load_project_workflow(
                    jira,
                    project_key,
                    workflow_name,
                    definition_reader=DefinitionPublisher(project_key),
                )
            if not workflow_instance.supports_ticket_type(ticket_type):
                raise ValueError(
                    f"workflow '{workflow_name}' is incompatible with ticket type "
                    f"'{ticket_type.value}'"
                )
        else:
            router = create_default_router()
            workflow_instance = router.resolve(
                ticket_type=ticket_type,
                labels=issue.labels,
                event={},
            )
    finally:
        await jira.close()

    if workflow_instance is None:
        raise ValueError(f"No workflow found for ticket type: {ticket_type}")

    # Build and compile workflow
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
        "yolo_mode": False,
    }
    if isinstance(workflow_instance, DeclarativeWorkflow):
        initial_state = {
            **workflow_instance.create_initial_state(ticket_key),
            **initial_state,
        }
        if checkpoint_values:
            status = workflow_instance.pin_status(checkpoint_values)
            if status == "pinned":
                workflow_instance.validate_pinned_state(checkpoint_values)
                initial_state = dict(checkpoint_values)
            elif status == "legacy_unpinned":
                raise WorkflowValidationError(
                    "checkpoint requires the explicit Phase 8 definition-pinning migration"
                )

    # Use ticket_key as thread_id for checkpointing
    config: dict[str, Any] = checkpoint_config
    if isinstance(workflow_instance, DeclarativeWorkflow):
        config["recursion_limit"] = 100

    effect_service = create_default_effect_service()
    identity = WorkflowIdentity(
        run_id=ticket_key,
        workflow_name=str(initial_state.get("workflow_name") or ticket_type_str),
        definition_revision=int(initial_state.get("workflow_definition_revision") or 1),
        definition_digest=initial_state.get("workflow_definition_digest"),
    )
    with bind_effect_runtime(effect_service, identity):
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
